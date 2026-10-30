"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const {
  SshConnectError,
  classifyPrompt,
  createSshSession,
  describeSshFailure,
  hostScript,
  installScript,
  isPortFree,
  loginScript,
  parseSignInLink,
  remoteCommand,
  runSshConnect,
  safeServerUrl,
  startSshConnect,
  validateDestination,
} = require("../src/ssh_connect");
const { IDENTITY_START, IDENTITY_END } = require("../src/arcaIdentity");

const SERVER = "https://example.cloud.databricks.com/omnigent";
const sleep = (ms) =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
const tick = () =>
  new Promise((resolve) => {
    setImmediate(resolve);
  });
const HOST_ID = "a".repeat(32);
const authorizeLink = (port) =>
  `https://example.cloud.databricks.com/oidc/v1/authorize?client_id=databricks-cli&redirect_uri=http%3A%2F%2Flocalhost%3A${port}&state=s1`;
const statusOutput = (serverUrl = SERVER) =>
  `\n${IDENTITY_START}\n${JSON.stringify({
    daemons: [
      {
        mode: "server",
        process: "online",
        host_status: "online",
        error: null,
        target: serverUrl,
        server_url: serverUrl,
        host_id: HOST_ID,
      },
    ],
  })}\n${IDENTITY_END}\n`;

describe("validateDestination", () => {
  it("accepts aliases, hosts and user@host", () => {
    for (const ok of ["my-ec2", "dev.example.com", "pat@10.0.0.4", "box_1"]) {
      assert.equal(validateDestination(` ${ok} `), ok);
    }
  });
  it("refuses anything ssh could read as an option or a shell could split", () => {
    for (const bad of ["-oProxyCommand=x", "a b", "a'b", 'a"b', "a;b", "", "@host", 42]) {
      assert.throws(() => validateDestination(bad), SshConnectError);
    }
  });
});

describe("remote scripts", () => {
  it("embed only allowlisted server URLs and pass through any login shell unchanged", () => {
    assert.throws(() => safeServerUrl("https://x.com/'$(id)'"), SshConnectError);
    assert.throws(() => safeServerUrl("ftp://x.com"), SshConnectError);
    assert.throws(() => remoteCommand("echo 'hi'"), /must not contain/);
    for (const script of [loginScript(SERVER), hostScript(SERVER), installScript()]) {
      assert.match(remoteCommand(script), /^sh -c '[^'\\]*'$/);
    }
    assert.match(loginScript(SERVER), /BROWSER=none \$group omnigent login "https:/);
  });
});

describe("loginScript on a real shell", () => {
  /** A fake `omnigent` in ~/.local/bin that starts a child, like the CLI it wraps. */
  function fakeHome(t, body) {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "ssh-login-test-"));
    t.after(() => fs.rmSync(home, { recursive: true, force: true }));
    fs.mkdirSync(path.join(home, ".local", "bin"), { recursive: true });
    const script = path.join(home, ".local", "bin", "omnigent");
    fs.writeFileSync(script, `#!/bin/sh\n${body}\n`, { mode: 0o755 });
    return home;
  }
  function runLogin(home, pathDirs = "/usr/bin:/bin") {
    const child = require("node:child_process").spawn("/bin/sh", ["-c", loginScript(SERVER)], {
      env: { PATH: pathDirs, HOME: home },
      stdio: ["pipe", "pipe", "pipe"],
    });
    const exited = new Promise((resolve) => {
      child.on("close", (code, signal) => resolve({ code, signal }));
    });
    return { child, exited };
  }

  // The login's CLI child ignores its parent's death, so both must be stopped,
  // including on minimal hosts without pkill. A survivor holds the pipes open,
  // so the timeout turns it into a failure instead of a hang.
  const toolSets = {
    "all tools": null,
    "no pkill": ["setsid"],
    "no setsid, as on macOS": ["pkill"],
  };
  for (const [label, tools] of Object.entries(toolSets)) {
    it(
      `stops the login and its child when the session's stdin closes (${label})`,
      { timeout: 15000 },
      async (t) => {
        const home = fakeHome(
          t,
          'echo $$ > "$HOME/login.pid"; sleep 300 & echo $! > "$HOME/child.pid"; wait',
        );
        let pathDirs;
        if (tools) {
          const bin = path.join(home, "bin");
          fs.mkdirSync(bin);
          for (const tool of ["sleep", ...tools]) {
            const found = ["/usr/bin", "/bin"]
              .map((dir) => path.join(dir, tool))
              .find((p) => fs.existsSync(p));
            if (!found) return t.skip(`${tool} not found`);
            fs.symlinkSync(found, path.join(bin, tool));
          }
          pathDirs = bin;
        }
        const { child, exited } = runLogin(home, pathDirs);
        const readPid = (name) => {
          const file = path.join(home, name);
          return fs.existsSync(file) ? Number(fs.readFileSync(file, "utf8")) || null : null;
        };
        // Polling is sequential by design.
        // oxlint-disable-next-line no-await-in-loop
        while (readPid("child.pid") === null) await sleep(20);
        const pids = [readPid("login.pid"), readPid("child.pid")];
        t.after(() => {
          for (const pid of pids) {
            try {
              process.kill(pid, "SIGKILL");
            } catch {
              // Already gone, as expected.
            }
          }
        });

        child.stdin.end();

        await exited;
        // An orphan stays a zombie until its new parent reaps it; that is still stopped.
        const stopped = (pid) => {
          try {
            process.kill(pid, 0);
          } catch {
            return true;
          }
          const stat = `/proc/${pid}/stat`;
          return fs.existsSync(stat) && / Z /.test(fs.readFileSync(stat, "utf8"));
        };
        for (let waited = 0; waited < 3000 && !pids.every(stopped); waited += 50) {
          // Polling is sequential by design.
          // oxlint-disable-next-line no-await-in-loop
          await sleep(50);
        }
        assert.deepEqual(
          pids.filter((pid) => !stopped(pid)),
          [],
        );
      },
    );
  }

  it("returns the login's own exit code, including 127 when omnigent is missing", async (t) => {
    const ok = runLogin(fakeHome(t, "exit 0"));
    assert.equal((await ok.exited).code, 0, "stdin still open: the watchdog must not block exit");
    ok.child.stdin.end();
    const missing = fs.mkdtempSync(path.join(os.tmpdir(), "ssh-login-test-"));
    t.after(() => fs.rmSync(missing, { recursive: true, force: true }));
    const absent = runLogin(missing);
    assert.equal((await absent.exited).code, 127);
    absent.child.stdin.end();
  });
});

describe("parseSignInLink", () => {
  it("reads the loopback port from a Databricks sign-in link, through ANSI codes", () => {
    assert.deepEqual(parseSignInLink(`\x1b[2m${authorizeLink(8021)}\x1b[0m\r`), {
      url: authorizeLink(8021),
      port: 8021,
    });
  });
  it("opens a server's own sign-in link without a forward", () => {
    assert.deepEqual(parseSignInLink(`Opening browser for login: ${SERVER}/auth/cli?t=1`), {
      url: `${SERVER}/auth/cli?t=1`,
      port: null,
    });
  });
  it("ignores other lines", () => {
    assert.equal(parseSignInLink("Please complete authentication by opening this link"), null);
    assert.equal(parseSignInLink("https://example.com/login"), null);
  });
});

describe("classifyPrompt", () => {
  it("tells host-key questions, secrets and other questions apart", () => {
    assert.equal(
      classifyPrompt("Are you sure you want to continue connecting (yes/no/[fingerprint])? "),
      "confirm",
    );
    assert.equal(classifyPrompt("Allow use of key?", "confirm"), "confirm");
    assert.equal(classifyPrompt("pat@my-ec2's password: "), "secret");
    assert.equal(
      classifyPrompt("Enter passphrase for key '/home/pat/.ssh/id_ed25519': "),
      "secret",
    );
    assert.equal(classifyPrompt("Passcode or option (1-3): "), "secret");
    assert.equal(classifyPrompt("Which project? "), "text");
  });
});

describe("describeSshFailure", () => {
  it("names the fix for each common ssh failure", () => {
    const cases = [
      ["pat@my-ec2: Permission denied (publickey).", /didn't accept your sign-in/],
      ["Host key verification failed.", /wasn't trusted/],
      ["@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@", /host key changed/],
      ["ssh: Could not resolve hostname my-ec2: nodename nor servname", /Couldn't find a machine/],
      ["ssh: connect to host 10.0.0.4 port 22: Connection timed out", /Couldn't reach my-ec2/],
    ];
    for (const [stderr, expected] of cases) {
      assert.match(describeSshFailure(255, stderr, "my-ec2"), expected);
    }
  });
});

/** Scripted stand-in for createSshSession that records every call in order. */
function fakeSession({ logins, host = { code: 0, stdout: statusOutput(), stderr: "" }, install }) {
  const calls = [];
  let loginRuns = 0;
  const control = (name) => async (port) => {
    calls.push(`${name} ${port}`);
    return { code: 0, stderr: "" };
  };
  return {
    calls,
    open: async () => {
      calls.push("open");
    },
    close: async () => {
      calls.push("close");
    },
    abort: () => calls.push("abort"),
    forwardLocal: control("forwardLocal"),
    cancelLocal: control("cancelLocal"),
    holdRemote: control("holdRemote"),
    releaseRemote: control("releaseRemote"),
    exec(script, opts = {}) {
      let finish;
      const promise = new Promise((resolve) => {
        finish = resolve;
      });
      let canceled = false;
      const cancel = () => {
        canceled = true;
        calls.push("cancel-exec");
        finish({ code: null, stdout: "", stderr: "", timedOut: false });
      };
      if (script.includes("omnigent login")) {
        const step = logins[loginRuns];
        loginRuns += 1;
        calls.push("exec login");
        void (async () => {
          // One line per tick, like a CLI writing as it goes.
          /* oxlint-disable no-await-in-loop */
          for (const line of step.lines ?? []) {
            opts.onStdout(`${line}\n`);
            await tick();
          }
          /* oxlint-enable no-await-in-loop */
          // Let the queued link handling (forward, browser) run before the CLI exits.
          await sleep(10);
          if (!canceled) finish({ code: step.code, stdout: "", stderr: step.stderr ?? "" });
        })();
      } else if (script.includes("omnigent host")) {
        calls.push("exec host");
        finish({ ...host, timedOut: false });
      } else {
        calls.push(`exec install stdin=${opts.stdin}`);
        finish({
          code: install?.code ?? 0,
          stdout: "",
          stderr: install?.stderr ?? "",
          timedOut: false,
        });
      }
      return { promise, cancel };
    },
  };
}

function connectContext(session, overrides = {}) {
  const opened = [];
  const statuses = [];
  return {
    opened,
    statuses,
    ctx: {
      serverUrl: SERVER,
      destination: "my-ec2",
      session,
      openExternal: async (url) => {
        session.calls.push(`open-browser ${url}`);
        opened.push(url);
      },
      onStatus: (status) =>
        statuses.push(status.code ? `${status.step} ${status.code}` : status.step),
      onOutput: () => {},
      askInstall: async () => true,
      readInstallScript: () => "INSTALLER",
      isPortFree: async () => true,
      ...overrides,
    },
  };
}

describe("runSshConnect", () => {
  it("holds the laptop's busy ports, takes the CLI's port before it starts, then starts the host", async () => {
    const session = fakeSession({
      logins: [{ lines: ["Signing in", authorizeLink(8021)], code: 0 }],
    });
    const { ctx, statuses } = connectContext(session, {
      isPortFree: async (port) => port !== 8020,
    });

    const result = await runSshConnect(ctx);

    assert.deepEqual(result, {
      ok: true,
      alreadyRunning: false,
      identity: { serverUrl: SERVER, hostId: HOST_ID },
    });
    assert.deepEqual(session.calls, [
      "open",
      "holdRemote 8020",
      "forwardLocal 8021",
      "exec login",
      `open-browser ${authorizeLink(8021)}`,
      "cancelLocal 8021",
      "releaseRemote 8020",
      "exec host",
      "close",
    ]);
    assert.deepEqual(statuses, [
      "connecting",
      "signing-in",
      "waiting-for-browser",
      "starting-host",
    ]);
  });

  it("opens a server's own sign-in link as is", async () => {
    const link = `${SERVER}/auth/cli?t=1`;
    const session = fakeSession({
      logins: [
        {
          lines: [`Opening browser for login: ${link}`, "Confirm this code in the browser: AB-12"],
          code: 0,
        },
      ],
    });
    const { ctx, opened, statuses } = connectContext(session);

    assert.equal((await runSshConnect(ctx)).ok, true);
    assert.deepEqual(opened, [link]);
    // Only the port taken up front, released once sign-in ends.
    assert.deepEqual(
      session.calls.filter((call) => /^(forward|cancel)Local/.test(call)),
      ["forwardLocal 8020", "cancelLocal 8020"],
    );
    // The CLI prints its code after the link; the user needs it to finish.
    assert.deepEqual(statuses, [
      "connecting",
      "signing-in",
      "waiting-for-browser",
      "confirm-code AB-12",
      "starting-host",
    ]);
  });

  it("asks before installing a missing CLI, pipes the bundled installer, then signs in again", async () => {
    const session = fakeSession({
      logins: [
        { lines: ["sh: 1: exec: omnigent: not found"], code: 127 },
        { lines: [authorizeLink(8020)], code: 0 },
      ],
    });
    const { ctx, statuses } = connectContext(session);

    assert.equal((await runSshConnect(ctx)).ok, true);
    assert.deepEqual(
      session.calls.filter((call) => call.startsWith("exec")),
      ["exec login", "exec install stdin=INSTALLER", "exec login", "exec host"],
    );
    assert.ok(statuses.includes("installing"));
  });

  it("stops without installing when the user declines", async () => {
    const session = fakeSession({ logins: [{ lines: [], code: 127 }] });
    const { ctx } = connectContext(session, { askInstall: async () => false });

    const result = await runSshConnect(ctx);

    assert.equal(result.ok, false);
    assert.equal(result.canceled, true);
    assert.ok(!session.calls.some((call) => call.startsWith("exec install")));
    assert.equal(session.calls.at(-1), "close");
  });

  it("keeps the port it took when another tool forwards the CLI's port mid sign-in", async () => {
    let probes = 0;
    const session = fakeSession({ logins: [{ lines: [authorizeLink(8020)], code: 0 }] });
    const { ctx, opened } = connectContext(session, {
      // Free during the busy-port scan, then held (by this connect) when the link arrives.
      isPortFree: async () => {
        probes += 1;
        return probes <= 21;
      },
    });

    assert.equal((await runSshConnect(ctx)).ok, true);
    assert.deepEqual(opened, [authorizeLink(8020)]);
  });

  it("moves the forward when the CLI skips a port taken on the remote machine", async () => {
    const session = fakeSession({ logins: [{ lines: [authorizeLink(8021)], code: 0 }] });
    const { ctx, opened } = connectContext(session);

    assert.equal((await runSshConnect(ctx)).ok, true);
    assert.deepEqual(opened, [authorizeLink(8021)]);
    assert.deepEqual(
      session.calls.filter((call) => /^(forward|cancel)Local/.test(call)),
      ["forwardLocal 8020", "cancelLocal 8020", "forwardLocal 8021", "cancelLocal 8021"],
    );
  });

  it("stops sign-in when the CLI's moved-to port is busy on the laptop", async () => {
    let probes = 0;
    const session = fakeSession({ logins: [{ lines: [authorizeLink(8021)], code: 0 }] });
    const { ctx, opened } = connectContext(session, {
      // Free during the busy-port scan, taken by the time the link arrives.
      isPortFree: async () => {
        probes += 1;
        return probes <= 21;
      },
    });

    const result = await runSshConnect(ctx);

    assert.equal(result.ok, false);
    assert.match(result.error, /Port 8021 became busy/);
    assert.deepEqual(opened, []);
    assert.ok(session.calls.includes("cancel-exec"));
  });

  // Every callback port is busy here: only a sign-in that needs no callback port can work.
  const allBusy = {
    "a server's own sign-in, which needs no callback port, goes through": [
      `Opening browser for login: ${SERVER}/auth/cli?t=1`,
      { ok: true, alreadyRunning: false, identity: { serverUrl: SERVER, hostId: HOST_ID } },
    ],
    "a Databricks sign-in, which does, stops with a plain message": [
      authorizeLink(8020),
      { ok: false, error: "No free sign-in port on this computer: 8020 to 8040 are all in use." },
    ],
  };
  for (const [label, [line, expected]] of Object.entries(allBusy)) {
    it(`with every sign-in port busy, ${label}`, async () => {
      const session = fakeSession({ logins: [{ lines: [line], code: 0 }] });
      const { ctx, opened } = connectContext(session, { isPortFree: async () => false });

      assert.deepEqual(await runSshConnect(ctx), expected);
      assert.deepEqual(opened, expected.ok ? [`${SERVER}/auth/cli?t=1`] : []);
      // Nothing held or forwarded: no port here could take the callback.
      assert.ok(!session.calls.some((call) => /^(holdRemote|forwardLocal)/.test(call)));
    });
  }

  it("stops sign-in at once when the link can't be opened", async () => {
    // The CLI would otherwise wait for a redirect that can never come.
    const session = fakeSession({ logins: [{ lines: [authorizeLink(8020)], code: 0 }] });
    const { ctx } = connectContext(session, {
      openExternal: async () => {
        throw new Error("no default browser");
      },
    });

    const result = await runSshConnect(ctx);

    assert.deepEqual(result, { ok: false, error: "Sign-in failed: no default browser" });
    assert.ok(session.calls.includes("cancel-exec"));
    assert.ok(session.calls.includes("cancelLocal 8020"));
  });

  // What the CLI printed, and what the failure says; link values never appear.
  const failures = {
    "the last plain output line, never a sign-in link": [
      ["Could not reach server", "https://x.example/secret?t=1"],
      "Could not reach server",
    ],
    "the CLI's own error over a trailing hint, without link values": [
      [
        "Error: Could not reach https://ws.example/oidc/authorize?state=s3cret for sign-in",
        "Is the server running?",
        "If this is a runner tunnel rejection (HTTP 401), stale host processes may be the cause.",
      ],
      "Could not reach https://ws.example/oidc/authorize for sign-in",
    ],
    "an error the CLI prints after the sign-in link": [
      [authorizeLink(8020), "Error: Authentication was denied."],
      "Authentication was denied.",
    ],
  };
  for (const [label, [lines, detail]] of Object.entries(failures)) {
    it(`reports ${label}`, async () => {
      const session = fakeSession({ logins: [{ lines, code: 1 }] });
      const { ctx } = connectContext(session);

      assert.deepEqual(await runSshConnect(ctx), {
        ok: false,
        error: `Signing in on my-ec2 failed: ${detail}`,
      });
    });
  }

  it("explains servers that sign in with a username and password", async () => {
    const session = fakeSession({
      logins: [{ lines: ["Username [admin]: ", "Aborted!"], code: 1 }],
    });
    const { ctx } = connectContext(session);

    assert.match((await runSshConnect(ctx)).error, /username and password/);
  });

  it("reports a host that didn't start", async () => {
    const session = fakeSession({
      logins: [{ lines: [], code: 0 }],
      host: { code: 1, stdout: "", stderr: "Error: not signed in" },
    });
    const { ctx } = connectContext(session);

    assert.equal(
      (await runSshConnect(ctx)).error,
      "Starting the Omnigent host on my-ec2 failed: not signed in",
    );
  });
});

describe("startSshConnect", () => {
  it("routes prompts through hooks, returns ssh's failure as a message, and removes its temp dir", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ssh-connect-test-"));
    let onPrompt;
    const prompts = [];
    const asked = new AbortController();
    const { promise } = startSshConnect(SERVER, "my-ec2", {
      hooks: {
        onOutput: () => {},
        onStatus: () => {},
        prompt: async (request) => {
          prompts.push(request);
          return "pw";
        },
        askInstall: async () => false,
      },
      openExternal: () => {},
      readInstallScript: () => null,
      makeTempDir: () => dir,
      createAskpass: async (options) => {
        onPrompt = options.onPrompt;
        return { env: { SSH_ASKPASS: "x" }, close: () => {} };
      },
      createSession: () => ({
        open: async () => {
          const request = { prompt: "pat@my-ec2's password: ", hint: "", signal: asked.signal };
          assert.equal(await onPrompt(request), "pw");
          throw new SshConnectError(describeSshFailure(255, "Permission denied", "my-ec2"));
        },
        close: async () => {},
        abort: () => {},
      }),
    });

    const result = await promise;

    assert.deepEqual(prompts, [
      { kind: "secret", text: "pat@my-ec2's password: ", signal: asked.signal },
    ]);
    assert.equal(prompts[0].signal, asked.signal);
    assert.equal(result.ok, false);
    assert.match(result.error, /didn't accept your sign-in/);
    assert.equal(fs.existsSync(dir), false);
  });

  it("stops a connect that is still opening and removes its temp dir", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ssh-connect-test-"));
    let opening;
    let rejectOpen;
    const opened = new Promise((resolve) => {
      opening = resolve;
    });
    let aborted = false;
    const connect = startSshConnect(SERVER, "my-ec2", {
      hooks: { onOutput: () => {}, onStatus: () => {}, prompt: async () => null },
      openExternal: () => {},
      readInstallScript: () => null,
      makeTempDir: () => dir,
      createAskpass: async () => ({ env: {}, close: () => {} }),
      createSession: () => ({
        open: () => {
          opening();
          return new Promise((_, reject) => {
            rejectOpen = reject;
          });
        },
        close: async () => {},
        abort: () => {
          aborted = true;
          // Killing the master ends open() with ssh's own failure.
          rejectOpen(new SshConnectError("ssh exited"));
        },
      }),
    });

    await opened;
    connect.cancel();

    assert.deepEqual(await connect.promise, {
      ok: false,
      canceled: true,
      error: "Connecting was canceled.",
    });
    assert.equal(aborted, true);
    assert.equal(fs.existsSync(dir), false);
  });

  it("starts no sign-in and opens no browser once canceled after connecting", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ssh-connect-test-"));
    const session = fakeSession({ logins: [{ lines: [`${SERVER}/auth/cli?t=1`], code: 0 }] });
    const opened = [];
    let probing;
    const probed = new Promise((resolve) => {
      probing = resolve;
    });
    let releaseProbe;
    const probeGate = new Promise((resolve) => {
      releaseProbe = resolve;
    });
    const connect = startSshConnect(SERVER, "my-ec2", {
      hooks: { onOutput: () => {}, onStatus: () => {}, prompt: async () => null },
      openExternal: (url) => opened.push(url),
      readInstallScript: () => null,
      makeTempDir: () => dir,
      createAskpass: async () => ({ env: {}, close: () => {} }),
      createSession: () => session,
      // The connection is up; the window closes while the sign-in ports are checked.
      isPortFree: async () => {
        probing();
        await probeGate;
        return true;
      },
    });

    await probed;
    connect.cancel();
    releaseProbe();

    assert.deepEqual(await connect.promise, {
      ok: false,
      canceled: true,
      error: "Connecting was canceled.",
    });
    assert.ok(!session.calls.includes("exec login"), session.calls.join(", "));
    assert.deepEqual(opened, []);
  });
});

describe("isPortFree", () => {
  it("sees a port held on either loopback address", async (t) => {
    for (const host of ["127.0.0.1", "::1"]) {
      const server = net.createServer();
      // A listener left open by a failed assertion would keep the run alive.
      t.after(() => server.close());
      // One address at a time is the point of the test.
      // oxlint-disable-next-line no-await-in-loop
      const listening = await new Promise((resolve) => {
        server.once("error", () => resolve(false));
        server.listen(0, host, () => resolve(true));
      });
      if (!listening) {
        t.diagnostic(`${host} isn't available here`);
        continue;
      }
      const { port } = server.address();
      // oxlint-disable-next-line no-await-in-loop
      assert.equal(await isPortFree(port), false, `held on ${host}`);
      // oxlint-disable-next-line no-await-in-loop
      await new Promise((resolve) => {
        server.close(resolve);
      });
      // oxlint-disable-next-line no-await-in-loop
      assert.equal(await isPortFree(port), true, `released on ${host}`);
    }
  });
});

describe("createSshSession", () => {
  it("reports a missing ssh as a plain message", async () => {
    const session = createSshSession({
      destination: "my-ec2",
      socketPath: "/tmp/s/m",
      spawn: () => {
        throw Object.assign(new Error("spawn ssh ENOENT"), { code: "ENOENT" });
      },
    });

    await assert.rejects(session.open(), {
      name: "Error",
      message: "Couldn't find ssh on this computer.",
    });
  });

  // Either way the master is gone, and ssh would otherwise quietly reconnect on its own.
  const stops = {
    "it is aborted": [(session) => session.abort(), { code: null, stderr: "" }],
    "its connection exits after opening": [
      (_session, master) => master.emit("exit", 255),
      { code: 255, stderr: "the connection closed" },
    ],
  };
  for (const [label, [stop, refused]] of Object.entries(stops)) {
    it(`starts no ssh once ${label}`, async () => {
      const spawned = [];
      let master;
      const spawn = (command, args) => {
        const child = new EventEmitter();
        child.stdout = new EventEmitter();
        child.stderr = new EventEmitter();
        child.kill = () => child.emit("close", null);
        spawned.push(args.includes("-M") ? "master" : args.join(" "));
        if (args.includes("-M")) master = child;
        else setImmediate(() => child.emit("close", 0));
        return child;
      };
      const session = createSshSession({
        destination: "my-ec2",
        socketPath: "/tmp/s/m",
        spawn,
        pathExists: () => true,
      });
      await session.open();
      const before = spawned.length;

      stop(session, master);

      const forward = await session.forwardLocal(8020);
      const run = await session.exec("true").promise;
      assert.deepEqual([forward.code, forward.stderr], [refused.code, refused.stderr]);
      assert.deepEqual([run.code, run.stderr], [refused.code, refused.stderr]);
      assert.equal(spawned.length, before);
    });
  }

  it("doesn't start the host when the connection drops right after sign-in", async () => {
    const spawned = [];
    let master;
    const spawn = (command, args) => {
      const child = new EventEmitter();
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      child.kill = () => child.emit("close", null);
      const line = args.join(" ");
      spawned.push(line);
      if (args.includes("-M")) {
        master = child;
        return child;
      }
      setImmediate(() => {
        // Sign-in succeeds, then the shared connection drops before the next step.
        if (line.includes("omnigent login")) master.emit("exit", 255);
        child.emit("close", 0);
      });
      return child;
    };
    const session = createSshSession({
      destination: "my-ec2",
      socketPath: "/tmp/s/m",
      spawn,
      pathExists: () => true,
    });
    const { ctx } = connectContext(session);

    const result = await runSshConnect(ctx);

    assert.deepEqual(result, {
      ok: false,
      error: "Starting the Omnigent host on my-ec2 failed: the connection closed",
    });
    assert.ok(spawned.some((line) => line.includes("omnigent login")));
    assert.ok(!spawned.some((line) => line.includes("omnigent host")), spawned.join("\n"));
  });

  it("opens one shared connection and sends forwards over it", async () => {
    const spawned = [];
    const spawn = (command, args) => {
      const child = new EventEmitter();
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      child.kill = () => child.emit("close", null);
      spawned.push([command, ...args]);
      // The master stays up; every client exits 0 straight away.
      if (!args.includes("-M")) setImmediate(() => child.emit("close", 0));
      return child;
    };
    const session = createSshSession({
      destination: "my-ec2",
      socketPath: "/tmp/s/m",
      spawn,
      pathExists: () => true,
    });

    await session.open();
    await session.forwardLocal(8021);
    await session.cancelLocal(8021);
    await session.holdRemote(8020);
    await session.exec("true").promise;

    // The user's config reaches the connection and commands, but never its ControlMaster,
    // forwards or RemoteCommand; control requests skip the config so it can't empty
    // theirs, and local forwards bind loopback whatever GatewayPorts says.
    const control = ["ssh", "-F", "/dev/null", "-S", "/tmp/s/m", "-O"];
    assert.deepEqual(spawned, [
      [
        "ssh",
        "-M",
        "-S",
        "/tmp/s/m",
        "-o",
        "ControlPersist=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ConnectTimeout=20",
        "-N",
        "my-ec2",
      ],
      [...control, "check", "my-ec2"],
      [...control, "forward", "-L", "localhost:8021:127.0.0.1:8021", "my-ec2"],
      [...control, "cancel", "-L", "localhost:8021:127.0.0.1:8021", "my-ec2"],
      [...control, "forward", "-R", "8020:127.0.0.1:9", "my-ec2"],
      [
        "ssh",
        "-S",
        "/tmp/s/m",
        "-o",
        "ControlMaster=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "RemoteCommand=none",
        "-T",
        "my-ec2",
        "sh -c 'true'",
      ],
    ]);
  });
});
