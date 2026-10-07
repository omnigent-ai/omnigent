"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const { MessageChannel } = require("node:worker_threads");
const { ARCA_PREVIEW_TIMEOUT_MS } = require("../src/arcaPreviewConfig");
const {
  createArcaPreviewManager,
  loopbackPreview,
  parseStatusJson,
  safeCommandDetail,
  sameServer,
} = require("../src/arcaPreview");

function holdFakeChildProcessOpen(t) {
  const channel = new MessageChannel();
  channel.port1.on("message", () => {});
  t.after(() => {
    channel.port1.close();
    channel.port2.close();
  });
}

function child({ exitOnKill = true } = {}) {
  const value = new EventEmitter();
  value.stdout = new EventEmitter();
  value.stderr = new EventEmitter();
  value.stderr.resume = () => (value.stderr.resumed = true);
  value.killed = false;
  value.exitCode = null;
  value.killSignals = [];
  value.on("exit", (code) => {
    value.exitCode = code;
  });
  value.kill = (signal) => {
    value.killed = true;
    value.killSignals.push(signal);
    if (exitOnKill && value.exitCode == null) {
      value.signalCode = signal;
      value.emit("exit", null, signal);
    }
  };
  return value;
}

function successfulSpawner({
  hostId = "host_arca",
  serverUrl = "https://srv.example.com/",
  hostStatus = "online",
  failForwardIndex = 0,
} = {}) {
  const calls = [];
  const children = [];
  let forwardIndex = 0;
  const spawn = (file, args) => {
    const proc = child();
    calls.push({ file, args });
    children.push(proc);
    if (args.includes("status")) {
      queueMicrotask(() => {
        proc.stdout.emit(
          "data",
          JSON.stringify({
            daemons: [
              {
                host_id: hostId,
                server_url: serverUrl,
                process: "online",
                ...(hostStatus === null ? {} : { host_status: hostStatus }),
              },
            ],
          }),
        );
        proc.emit("exit", 0);
      });
    } else if (args.includes("-O")) {
      forwardIndex += 1;
      queueMicrotask(() => {
        if (forwardIndex === failForwardIndex) {
          proc.stderr.emit(
            "data",
            "mux_client_forward: forwarding request failed: Port forwarding failed\n" +
              "muxclient: master forward request failed\n",
          );
          proc.emit("exit", 255);
        } else proc.emit("exit", 0);
      });
    }
    return proc;
  };
  return { spawn, calls, children };
}

describe("Arca localhost preview URL", () => {
  it("shares a bounded budget that accommodates cold Arca startup", () => {
    assert.equal(ARCA_PREVIEW_TIMEOUT_MS, 60_000);
  });

  it("accepts explicit loopback previews and preserves their exact origin", () => {
    assert.deepEqual(loopbackPreview("http://localhost:5173/app"), {
      origin: "http://localhost:5173",
      host: "localhost",
      port: 5173,
    });
    assert.deepEqual(loopbackPreview("http://127.0.0.1:7331/"), {
      origin: "http://127.0.0.1:7331",
      host: "127.0.0.1",
      port: 7331,
    });
    assert.equal(loopbackPreview("https://example.com"), null);
    assert.equal(loopbackPreview("http://10.0.0.2:5173"), null);
    assert.equal(loopbackPreview("http://[::1]:5173"), null);
  });

  it("parses status JSON after Arca startup notices", () => {
    assert.deepEqual(parseStatusJson('Starting Arca…\n{"daemons":[]}'), { daemons: [] });
  });

  it("strips terminal escape and control sequences from command details", () => {
    assert.equal(
      safeCommandDetail("\u001b[31mbind failed\u001b[0m\u0000\u001b]0;secret\u0007"),
      "bind failed",
    );
  });

  it("matches workspace UI and API mounts without relaxing host or explicit selectors", () => {
    assert.equal(
      sameServer(
        "https://acme.cloud.databricks.com/api/2.0/omnigent",
        "https://acme.cloud.databricks.com/omnigent?o=123",
      ),
      true,
    );
    assert.equal(
      sameServer(
        "https://acme.cloud.databricks.com/api/2.0/omnigent?o=456",
        "https://acme.cloud.databricks.com/omnigent?o=123",
      ),
      false,
    );
    assert.equal(
      sameServer(
        "https://other.cloud.databricks.com/api/2.0/omnigent",
        "https://acme.cloud.databricks.com/omnigent",
      ),
      false,
    );
  });
});

describe("Arca preview manager", () => {
  it("uses a private control socket short enough for Darwin temporary suffixes", async () => {
    const fake = successfulSpawner();
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: fake.spawn,
      socketReady: () => true,
    });
    const owned = await manager.prepare({
      conversationId: "short-socket",
      url: "http://localhost:5173",
      hostId: "host_arca",
      serverUrl: "https://srv.example.com",
    });
    const master = fake.calls.find((call) => call.args.includes("-M"));
    const socketPath = master.args[master.args.indexOf("-S") + 1];
    assert.ok(Buffer.byteLength(`${socketPath}.XXXXXXXXXX`) < 104);
    assert.equal(fs.statSync(require("node:path").dirname(socketPath)).mode & 0o777, 0o700);
    const shutdown = owned.release();
    const exit = fake.calls.at(-1);
    assert.equal(exit.file, "/usr/bin/ssh");
    assert.deepEqual(exit.args, [
      "-F",
      "/dev/null",
      "-o",
      "ProxyCommand=/usr/bin/false",
      "-o",
      "BatchMode=yes",
      "-o",
      "ControlMaster=no",
      "-S",
      socketPath,
      "-O",
      "exit",
      "arca-preview.invalid",
    ]);
    assert.equal(fs.existsSync(require("node:path").dirname(socketPath)), true);
    await shutdown;
    assert.equal(fs.existsSync(require("node:path").dirname(socketPath)), false);
  });

  it("verifies the exact server host then starts requested independent forwards", async () => {
    const serverUrl = "https://srv.example.com/omnigent?o=123";
    const fake = successfulSpawner({ serverUrl });
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/usr/local/bin/arca",
      spawnFn: fake.spawn,
      socketReady: () => true,
    });
    const first = await manager.prepare({
      conversationId: "a",
      url: "http://localhost:5173/app",
      hostId: "host_arca",
      serverUrl,
    });
    const second = await manager.prepare({
      conversationId: "b",
      url: "http://localhost:7331/",
      hostId: "host_arca",
      serverUrl,
    });

    assert.equal(first.origin, "http://localhost:5173");
    assert.equal(second.origin, "http://localhost:7331");
    const statusCalls = fake.calls.filter((call) => call.args.includes("status"));
    assert.equal(statusCalls.length, 2);
    assert.ok(statusCalls.every((call) => call.file === "/usr/local/bin/arca"));
    assert.ok(
      statusCalls.every(
        (call) => call.args[call.args.indexOf("--server") + 1] === `'${serverUrl}'`,
      ),
    );
    const forwards = fake.calls.filter((call) => call.args.includes("-L"));
    assert.ok(forwards.every((call) => call.file === "/usr/bin/ssh"));
    assert.ok(forwards.every((call) => call.args.includes("ProxyCommand=/usr/bin/false")));
    assert.ok(forwards.every((call) => call.args.includes("BatchMode=yes")));
    assert.ok(forwards.every((call) => call.args.includes("ControlMaster=no")));
    assert.deepEqual(
      forwards.map((call) => call.args[call.args.indexOf("-L") + 1]),
      [
        "127.0.0.1:5173:localhost:5173",
        "[::1]:5173:localhost:5173",
        "127.0.0.1:7331:localhost:7331",
        "[::1]:7331:localhost:7331",
      ],
    );
    assert.ok(forwards.every((call) => call.args.includes("/dev/null")));
    assert.ok(forwards.every((call) => call.args.at(-1) === "arca-preview.invalid"));
    const masters = fake.calls.filter((call) => call.args.includes("-M"));
    assert.ok(masters.every((call) => call.file === "/usr/local/bin/arca"));
    assert.ok(masters.every((call) => call.args[0] === "ssh"));
    assert.ok(masters.every((call) => call.args.includes("ClearAllForwardings=yes")));
    assert.ok(masters.every((call) => call.args.includes("ControlPersist=no")));
    assert.equal(
      fake.children[fake.calls.findIndex((call) => call.args.includes("-M"))].stderr.resumed,
      true,
    );
    await Promise.all([first.release(), second.release()]);
  });

  it("routes a scoped status probe and requires an online exact host attestation", async () => {
    const serverUrl = "https://srv.example.com/omnigent?o=123";
    for (const hostStatus of ["offline", null]) {
      const fake = successfulSpawner({ serverUrl: "https://srv.example.com/omnigent", hostStatus });
      const manager = createArcaPreviewManager({
        resolveArcaPathFn: () => "/arca",
        spawnFn: fake.spawn,
        socketReady: () => true,
      });
      // oxlint-disable-next-line no-await-in-loop -- each status owns isolated manager state.
      await assert.rejects(
        manager.prepare({
          conversationId: `scoped-${String(hostStatus)}`,
          url: "http://localhost:5173",
          hostId: "host_arca",
          serverUrl,
        }),
        /not running on this server's Arca host/,
      );
      const status = fake.calls.find((call) => call.args.includes("status"));
      assert.equal(status.args[status.args.indexOf("--server") + 1], `'${serverUrl}'`);
    }

    const online = successfulSpawner({ serverUrl: "https://srv.example.com/omnigent" });
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: online.spawn,
      socketReady: () => true,
    });
    const owned = await manager.prepare({
      conversationId: "scoped-online",
      url: "http://localhost:5173",
      hostId: "host_arca",
      serverUrl,
    });
    await owned.release();
  });

  it("bounds an unresponsive mux exit before killing the master and removing its socket", async () => {
    const fake = successfulSpawner();
    let exitChild;
    let unlinked = false;
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: (file, args) => {
        if (args.includes("-O") && args.includes("exit")) {
          exitChild = child({ exitOnKill: false });
          return exitChild;
        }
        return fake.spawn(file, args);
      },
      socketReady: () => true,
      socketPathFn: () => "/tmp/oa-test/s",
      unlinkSocket: () => {
        unlinked = true;
      },
      shutdownTimeoutMs: 5,
      terminationGraceMs: 5,
    });
    const owned = await manager.prepare({
      conversationId: "bounded-exit",
      url: "http://localhost:5173",
      hostId: "host_arca",
      serverUrl: "https://srv.example.com",
    });
    await owned.release();
    const masterIndex = fake.calls.findIndex((call) => call.args.includes("-M"));
    assert.equal(exitChild.killed, true);
    assert.deepEqual(exitChild.killSignals, ["SIGTERM", "SIGKILL"]);
    assert.equal(fake.children[masterIndex].killed, true);
    assert.equal(unlinked, true);
  });

  it("waits for an in-flight shutdown before rebinding a replacement", async () => {
    const fake = successfulSpawner();
    let delayedExit;
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: (file, args) => {
        if (!delayedExit && args.includes("-O") && args.includes("exit")) {
          delayedExit = child();
          return delayedExit;
        }
        return fake.spawn(file, args);
      },
      socketReady: () => true,
      terminationGraceMs: 5,
    });
    await manager.prepare({
      conversationId: "replace",
      url: "http://localhost:5173",
      hostId: "host_arca",
      serverUrl: "https://srv.example.com",
    });
    const replacement = manager.prepare({
      conversationId: "replace",
      url: "http://127.0.0.1:5173",
      hostId: "host_arca",
      serverUrl: "https://srv.example.com",
    });
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    assert.equal(fake.calls.filter((call) => call.args.includes("status")).length, 1);
    delayedExit.emit("exit", 0);
    const owned = await replacement;
    assert.equal(fake.calls.filter((call) => call.args.includes("status")).length, 2);
    await owned.release();
  });

  it("serializes same-port preparation behind another conversation's shutdown", async () => {
    const fake = successfulSpawner();
    let delayedExit;
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: (file, args) => {
        if (!delayedExit && args.includes("-O") && args.includes("exit")) {
          delayedExit = child();
          return delayedExit;
        }
        return fake.spawn(file, args);
      },
      socketReady: () => true,
      terminationGraceMs: 5,
    });
    const first = await manager.prepare({
      conversationId: "first",
      url: "http://localhost:5173",
      hostId: "host_arca",
      serverUrl: "https://srv.example.com",
    });
    const releasing = first.release();
    const secondPending = manager.prepare({
      conversationId: "second",
      url: "http://localhost:5173",
      hostId: "host_arca",
      serverUrl: "https://srv.example.com",
    });
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    assert.equal(fake.calls.filter((call) => call.args.includes("status")).length, 1);
    delayedExit.emit("exit", 0);
    await releasing;
    const second = await secondPending;
    assert.equal(fake.calls.filter((call) => call.args.includes("status")).length, 2);
    await second.release();
  });

  it("keeps released and pending work in shutdownAll until cleanup settles", async () => {
    const fake = successfulSpawner();
    let delayedExit;
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: (file, args) => {
        if (!delayedExit && args.includes("-O") && args.includes("exit")) {
          delayedExit = child();
          return delayedExit;
        }
        return fake.spawn(file, args);
      },
      socketReady: () => true,
      terminationGraceMs: 5,
    });
    const owned = await manager.prepare({
      conversationId: "released",
      url: "http://localhost:5173",
      hostId: "host_arca",
      serverUrl: "https://srv.example.com",
    });
    const firstRelease = owned.release();
    assert.equal(manager.release("released"), firstRelease);
    let settled = false;
    const all = manager.shutdownAll().then(() => (settled = true));
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    assert.equal(settled, false);
    delayedExit.emit("exit", 0);
    await all;
    assert.equal(settled, true);

    const pendingChild = child({ exitOnKill: false });
    const pendingManager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: () => pendingChild,
      terminationGraceMs: 5,
    });
    const attempt = pendingManager.prepare({
      conversationId: "pending",
      url: "http://localhost:7331",
      hostId: "host_arca",
      serverUrl: "https://srv.example.com",
    });
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    await pendingManager.shutdownAll();
    await assert.rejects(attempt, /cancelled|superseded/);
    assert.deepEqual(pendingChild.killSignals, ["SIGTERM", "SIGKILL"]);
  });

  it("rejects a different, offline, or unknown requesting host", async () => {
    const fake = successfulSpawner({ hostId: "other_host" });
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: fake.spawn,
      socketReady: () => true,
    });
    await assert.rejects(
      manager.prepare({
        conversationId: "a",
        url: "http://localhost:5173",
        hostId: "host_arca",
        serverUrl: "https://srv.example.com",
      }),
      /not running on this server's Arca host/,
    );
    await assert.rejects(
      manager.prepare({
        conversationId: "b",
        url: "http://localhost:5173",
        hostId: null,
        serverUrl: "https://srv.example.com",
      }),
      /host is unknown/,
    );
  });

  it("rejects a daemon that matches only the connected renderer server", async () => {
    const connectedServer = "https://connected.cloud.databricks.com/omnigent";
    const arcaTarget = "https://target.cloud.databricks.com/omnigent";
    const fake = successfulSpawner({ serverUrl: connectedServer });
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: fake.spawn,
      socketReady: () => true,
    });
    await assert.rejects(
      manager.prepare({
        conversationId: "target-mismatch",
        url: "http://localhost:5173",
        hostId: "host_arca",
        serverUrl: arcaTarget,
      }),
      /not running on this server's Arca host/,
    );
    const status = fake.calls.find((call) => call.args.includes("status"));
    assert.equal(status.args[status.args.indexOf("--server") + 1], `'${arcaTarget}'`);
  });

  it("fails an occupied desktop port without treating that listener as readiness", async () => {
    let master = null;
    const spawn = (_file, args) => {
      const proc = child();
      if (args.includes("-M")) master = proc;
      queueMicrotask(() => {
        if (args.includes("status")) {
          proc.stdout.emit(
            "data",
            JSON.stringify({
              daemons: [
                {
                  host_id: "host_arca",
                  server_url: "https://srv.example.com/",
                  process: "online",
                  host_status: "online",
                },
              ],
            }),
          );
          proc.emit("exit", 0);
        } else if (args.includes("forward")) {
          master?.stderr.emit("data", "bind [127.0.0.1]:5173: Address already in use\n");
          proc.stderr.emit(
            "data",
            "mux_client_forward: forwarding request failed: Port forwarding failed\n" +
              "muxclient: master forward request failed\n",
          );
          proc.emit("exit", 255);
        } else if (args.includes("-O")) {
          proc.emit("exit", 0);
        }
      });
      return proc;
    };
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: spawn,
      socketReady: () => true,
      logError: () => {},
    });
    await assert.rejects(
      manager.prepare({
        conversationId: "a",
        url: "http://localhost:5173",
        hostId: "host_arca",
        serverUrl: "https://srv.example.com",
      }),
      /could not open localhost preview port 5173 on IPv4 \(127\.0\.0\.1\)/,
    );
  });

  it("rolls back both localhost families when the second exact forward fails", async () => {
    const fake = successfulSpawner({ failForwardIndex: 2 });
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: fake.spawn,
      socketReady: () => true,
      logError: () => {},
    });
    await assert.rejects(
      manager.prepare({
        conversationId: "dual",
        url: "http://localhost:5173",
        hostId: "host_arca",
        serverUrl: "https://srv.example.com",
      }),
      /could not open localhost preview port 5173 on IPv6 \(\[::1\]\)/,
    );
    const masterIndex = fake.calls.findIndex((call) => call.args.includes("-M"));
    assert.equal(fake.children[masterIndex].killed, true);
  });

  it("cancels pending and active work, and reports an unexpected forward exit", async () => {
    const pending = child();
    const exits = [];
    let calls = 0;
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      onExit: (id) => exits.push(id),
      spawnFn: () => {
        calls += 1;
        if (calls === 1) return pending;
        throw new Error("unexpected spawn");
      },
    });
    const attempt = manager.prepare({
      conversationId: "a",
      url: "http://localhost:5173",
      hostId: "host_arca",
      serverUrl: "https://srv.example.com",
    });
    await manager.prepare({ conversationId: "a", url: "https://example.com" });
    assert.equal(calls, 0);
    await assert.rejects(attempt, /cancelled|superseded/);

    const fake = successfulSpawner();
    const active = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: fake.spawn,
      socketReady: () => true,
      onExit: (id) => exits.push(id),
    });
    const owned = await active.prepare({
      conversationId: "live",
      url: "http://localhost:5173",
      hostId: "host_arca",
      serverUrl: "https://srv.example.com",
    });
    fake.children[fake.calls.findIndex((call) => call.args.includes("-M"))].emit("exit", 1);
    assert.deepEqual(exits, ["live"]);
    await owned.release();
  });

  it("contains cleanup and callback failures on unexpected master exit", async () => {
    const fake = successfulSpawner();
    const logged = [];
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: fake.spawn,
      socketReady: () => true,
      socketPathFn: () => ({ socketPath: "/tmp/arca-review/s", socketDir: "/tmp/arca-review" }),
      unlinkSocket: () => {
        throw new Error("unlink denied");
      },
      removeSocketDir: () => {
        throw new Error("remove denied");
      },
      onExit: () => {
        throw new Error("close failed");
      },
      logError: (...args) => logged.push(args.join(" ")),
    });
    await manager.prepare({
      conversationId: "exit-errors",
      url: "http://localhost:5173",
      hostId: "host_arca",
      serverUrl: "https://srv.example.com",
    });
    const master = fake.children[fake.calls.findIndex((call) => call.args.includes("-M"))];
    assert.doesNotThrow(() => master.emit("exit", 1));
    assert.equal(manager.release("exit-errors"), null);
    assert.ok(logged.some((message) => message.includes("close failed")));
  });

  it("settles a preparation deadline and terminates the owned status process", async (t) => {
    holdFakeChildProcessOpen(t);
    const proc = child();
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: () => proc,
      timeoutMs: 5,
    });
    await assert.rejects(
      manager.prepare({
        conversationId: "slow",
        url: "http://localhost:5173",
        hostId: "host_arca",
        serverUrl: "https://srv.example.com",
      }),
      /timed out preparing/,
    );
    assert.equal(proc.killed, true);
  });

  it("does not schedule a late deadline kill after synchronous cancellation", async () => {
    let manager;
    let replacement;
    const proc = child({ exitOnKill: false });
    const spawnFn = () => {
      replacement = manager.prepare({
        conversationId: "sync-cancel",
        url: "https://example.com",
      });
      return proc;
    };
    manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn,
      timeoutMs: 5,
      terminationGraceMs: 5,
    });
    const attempt = manager.prepare({
      conversationId: "sync-cancel",
      url: "http://localhost:5173",
      hostId: "host_arca",
      serverUrl: "https://srv.example.com",
    });
    await assert.rejects(attempt, /cancelled|superseded/);
    await replacement;
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    assert.deepEqual(proc.killSignals, ["SIGTERM"]);
  });

  it("bounds captured command output", async () => {
    const proc = child();
    const logged = [];
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      logError: (...args) => logged.push(args.join(" ")),
      spawnFn: () => {
        queueMicrotask(() => {
          proc.stderr.emit("data", "x".repeat(20_000));
          proc.emit("exit", 1);
        });
        return proc;
      },
    });
    await assert.rejects(
      manager.prepare({
        conversationId: "noisy",
        url: "http://localhost:5173",
        hostId: "host_arca",
        serverUrl: "https://srv.example.com",
      }),
      (error) => error.message.length <= 8_192,
    );
    assert.ok(logged[0].length <= 8_240);
  });

  it("directly settles cancellation while waiting for the owned control socket", async () => {
    const fake = successfulSpawner();
    let socketPolls = 0;
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: fake.spawn,
      socketReady: () => {
        socketPolls += 1;
        return false;
      },
    });
    const pending = manager.prepare({
      conversationId: "socket-wait",
      url: "http://localhost:5173",
      hostId: "host_arca",
      serverUrl: "https://srv.example.com",
    });
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    const releasing = manager.release("socket-wait");
    await assert.rejects(pending, /cancelled/);
    const pollsAfterCancellation = socketPolls;
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    assert.equal(socketPolls, pollsAfterCancellation);
    await releasing;
  });

  it("stops control-socket polling after the master exits", async () => {
    const fake = successfulSpawner();
    let master;
    let socketPolls = 0;
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn: (file, args) => {
        const proc = fake.spawn(file, args);
        if (args.includes("-M")) master = proc;
        return proc;
      },
      socketReady: () => {
        socketPolls += 1;
        return false;
      },
    });
    const pending = manager.prepare({
      conversationId: "master-exit-socket-wait",
      url: "http://localhost:5173",
      hostId: "host_arca",
      serverUrl: "https://srv.example.com",
    });
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    master.emit("exit", 9);
    await assert.rejects(pending, /Arca preview exited \(9\)/);
    const pollsAfterExit = socketPolls;
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    assert.equal(socketPolls, pollsAfterExit);
  });

  it("fails immediately when the control master exits during a forward request", async () => {
    let master;
    const spawnFn = (_file, args) => {
      const proc = child();
      if (args.includes("status")) {
        queueMicrotask(() => {
          proc.stdout.emit(
            "data",
            JSON.stringify({
              daemons: [
                {
                  host_id: "host_arca",
                  server_url: "https://srv.example.com",
                  process: "online",
                  host_status: "online",
                },
              ],
            }),
          );
          proc.emit("exit", 0);
        });
      } else if (args.includes("-M")) {
        master = proc;
        setImmediate(() => proc.emit("exit", 9));
      }
      return proc;
    };
    const manager = createArcaPreviewManager({
      resolveArcaPathFn: () => "/arca",
      spawnFn,
      socketReady: () => true,
    });
    await assert.rejects(
      manager.prepare({
        conversationId: "master-exit",
        url: "http://localhost:5173",
        hostId: "host_arca",
        serverUrl: "https://srv.example.com",
      }),
      /Arca preview exited \(9\)/,
    );
    assert.equal(master.killed, false);
  });
});
