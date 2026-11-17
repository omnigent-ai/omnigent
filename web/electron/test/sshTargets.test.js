const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");

const { createWorkspaceSshForwards } = require("../src/workspaceSshForwards");
const { createSshTargetChecks } = require("../src/sshTargets");
const { workspaceSocketRequest } = require("../src/workspaceSocket");

const SERVER = "https://server.example.com/";
const METHOD = "/omnigent.api.workspace.v1alpha1.WorkspaceService/ReadFile";
const PROBE = "/omnigent.api.workspace.v1alpha1.WorkspaceService/ListDirectory";
const REMOTE = "/tmp/omnigent-workspace-ab12/workspace.sock";
const RETRY_MS = 5 * 60_000;
const unix = process.platform !== "win32";

/** A shared connection whose forwards answer like a host that accepts only `Bearer good`. */
function fakeSsh() {
  const servers = [];
  const fake = {
    sessions: [],
    // Drop the probe's connection instead of answering it.
    resetProbe: false,
    stop: () => servers.forEach((server) => server.close()),
  };
  fake.createSession = (options) => {
    const session = {
      options,
      closed: false,
      open: async () => {},
      forwardSocket: async (localPath) => {
        const server = http.createServer((req, res) => {
          session.paths = [...(session.paths ?? []), req.url];
          if (fake.resetProbe && req.url === PROBE) return req.socket.destroy();
          res.writeHead(req.headers.authorization === "Bearer good" ? 200 : 401);
          res.end();
        });
        servers.push(server);
        await new Promise((resolve) => {
          server.listen(localPath, resolve);
        });
        return { code: 0 };
      },
      close: async () => {
        session.closed = true;
      },
      abort: () => {},
    };
    fake.sessions.push(session);
    return session;
  };
  return fake;
}

describe("an SSH alias the page suggests", { skip: !unix && "needs Unix sockets" }, () => {
  let ssh;
  let forwards;
  let recorded;
  let asked;
  let answer;
  let clock;
  let routes;

  beforeEach(() => {
    ssh = fakeSsh();
    forwards = createWorkspaceSshForwards({ createSession: ssh.createSession });
    recorded = [];
    asked = [];
    answer = async () => true;
    clock = 0;
    const checks = createSshTargetChecks({
      record: (...entry) => recorded.push(entry),
      now: () => clock,
    });
    routes = {
      sshDestination: (hostId) => recorded.find(([, id]) => id === hostId)?.[2] ?? null,
      verifySshTarget: (hostId, alias, proves) =>
        checks.verify(SERVER, hostId, alias, {
          approve: () => {
            asked.push([hostId, alias]);
            return answer();
          },
          proves,
        }),
      forwards,
    };
  });

  afterEach(() => {
    forwards.closeAll();
    ssh.stop();
  });

  const call = (overrides = {}) =>
    workspaceSocketRequest(
      {
        hostId: "host-1",
        sshTarget: "my-box",
        socketPath: REMOTE,
        path: METHOD,
        headers: { authorization: "Bearer good" },
        body: new Uint8Array(),
        ...overrides,
      },
      routes,
    );

  it("is asked about once, and recorded once a token-checked call over it succeeds", async () => {
    assert.equal((await call()).status, 200);
    assert.equal((await call()).status, 200);

    assert.deepEqual(asked, [["host-1", "my-box"]]);
    assert.deepEqual(recorded, [[SERVER, "host-1", "my-box"]]);
    assert.deepEqual(ssh.sessions[0].paths, [PROBE, METHOD, METHOD]);
    assert.ok(ssh.sessions[0].options.sshArgs.includes("BatchMode=yes"));
  });

  it("runs no SSH, and isn't asked about again, when the user declines", async () => {
    answer = async () => false;

    await assert.rejects(call(), /not allowed/);
    await assert.rejects(call({ hostId: "host-2" }), /not allowed/);

    assert.deepEqual(asked, [["host-1", "my-box"]]);
    assert.equal(ssh.sessions.length, 0, "no connection is opened");
    assert.deepEqual(recorded, []);
  });

  it("is never offered when it isn't a plain alias for a known host", async () => {
    const refused = {
      "a user@host destination": { sshTarget: "me@my-box" },
      "an option": { sshTarget: "-oProxyCommand=x" },
      "no host": { hostId: undefined },
    };
    await Promise.all(
      Object.entries(refused).map(([name, overrides]) =>
        assert.rejects(call(overrides), /not a workspace socket/, name),
      ),
    );
    assert.deepEqual(asked, []);
    assert.equal(ssh.sessions.length, 0, "no connection is opened");
  });

  it("shares one question and one probe among concurrent reads, and holds other questions", async () => {
    let allow;
    answer = () =>
      new Promise((resolve) => {
        allow = resolve;
      });

    const reads = [call(), call()];
    await assert.rejects(call({ hostId: "host-2" }), /not allowed/);
    allow(true);

    assert.deepEqual(
      (await Promise.all(reads)).map((response) => response.status),
      [200, 200],
    );
    assert.deepEqual(asked, [["host-1", "my-box"]]);
    assert.deepEqual(ssh.sessions[0].paths, [PROBE, METHOD, METHOD]);
  });

  it("isn't recorded, or tried again for five minutes, when the host refuses the token there", async () => {
    await assert.rejects(call({ headers: { authorization: "Bearer other" } }), /not verified/);
    await assert.rejects(call(), /not verified/);

    assert.deepEqual(recorded, []);
    assert.equal(ssh.sessions.length, 1, "the second read doesn't connect again");
    assert.equal(ssh.sessions[0].closed, true, "the refused connection is closed");

    clock += RETRY_MS + 1;
    assert.equal((await call()).status, 200);
    assert.equal(asked.length, 1, "an allowed alias isn't asked about again");
    assert.deepEqual(recorded, [[SERVER, "host-1", "my-box"]]);
  });

  it("closes the connection, and waits, when the probe fails without an answer", async () => {
    ssh.resetProbe = true;

    await assert.rejects(call(), /not verified/);
    await assert.rejects(call(), /not verified/);

    assert.deepEqual(recorded, []);
    assert.equal(ssh.sessions.length, 1, "the second read doesn't connect again");
    assert.equal(ssh.sessions[0].closed, true, "the failed connection is closed");
  });

  it("is ignored when the host already has a recorded destination", async () => {
    routes.sshDestination = () => "recorded-box";

    await call({ sshTarget: "my-box" });

    assert.equal(ssh.sessions[0].options.destination, "recorded-box");
    assert.deepEqual(asked, []);
    assert.deepEqual(recorded, []);
  });
});

describe("createSshTargetChecks", () => {
  it("still uses a proven alias when recording it fails", async () => {
    const checks = createSshTargetChecks({
      record: () => {
        throw new Error("disk full");
      },
    });
    const steps = { approve: async () => true, proves: async () => true };

    assert.equal(await checks.verify(SERVER, "host-1", "my-box", steps), true);
  });
});
