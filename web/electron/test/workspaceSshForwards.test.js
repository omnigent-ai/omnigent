const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const { createWorkspaceSshForwards } = require("../src/workspaceSshForwards");
const { createSshHostStore } = require("../src/sshHosts");
const { workspaceSocketRequest } = require("../src/workspaceSocket");

const METHOD = "/omnigent.api.workspace.v1alpha1.WorkspaceService/ReadFile";
const REMOTE = "/tmp/omnigent-workspace-ab12_x/workspace.sock";
const unix = process.platform !== "win32";

/** The host's answer: echo the socket path and the authorization header. */
function answer(remotePath, req, res) {
  res.writeHead(200, { "content-type": "application/connect+proto" });
  res.end(`${remotePath} ${req.headers.authorization}`);
}

/**
 * A stand-in for ssh's shared connection. A forward serves HTTP on the local
 * socket, as the remote host would through a real `ssh -L`.
 */
function fakeSsh({ openError = null, openGate = null, forwardCode = 0, respond = answer } = {}) {
  const sessions = [];
  const servers = [];
  const createSession = (options) => {
    const session = {
      options,
      forwards: [],
      closed: false,
      aborted: false,
      open: async () => {
        await openGate;
        if (openError) throw new Error(openError);
      },
      forwardSocket: async (localPath, remotePath) => {
        session.forwards.push([localPath, remotePath]);
        if (forwardCode !== 0) return { code: forwardCode };
        const server = http.createServer((req, res) => respond(remotePath, req, res));
        servers.push(server);
        await new Promise((resolve) => {
          server.listen(localPath, resolve);
        });
        return { code: 0 };
      },
      close: async () => {
        session.closed = true;
      },
      abort: () => {
        session.aborted = true;
      },
    };
    sessions.push(session);
    return session;
  };
  return { sessions, createSession, stop: () => servers.forEach((server) => server.close()) };
}

const call = (routes, overrides = {}) =>
  workspaceSocketRequest(
    {
      hostId: "host-1",
      socketPath: REMOTE,
      path: METHOD,
      headers: { authorization: "Bearer t" },
      body: new Uint8Array(),
      ...overrides,
    },
    routes,
  );

describe("workspace calls over SSH", { skip: !unix && "needs Unix sockets" }, () => {
  let ssh;
  let forwards;
  let routes;

  beforeEach(() => {
    ssh = fakeSsh();
    forwards = createWorkspaceSshForwards({ createSession: ssh.createSession });
    routes = {
      sshDestination: (hostId) => (hostId === "host-1" ? "my-box" : null),
      forwards,
    };
  });

  afterEach(() => {
    forwards.closeAll();
    ssh.stop();
  });

  it("reaches a recorded host's socket through one prompt-free connection", async () => {
    const first = await call(routes);
    const second = await call(routes);

    assert.equal(Buffer.from(first.body).toString(), `${REMOTE} Bearer t`);
    assert.equal(second.status, 200);
    assert.equal(ssh.sessions.length, 1, "the connection is shared");
    const [session] = ssh.sessions;
    assert.equal(session.options.destination, "my-box");
    assert.ok(session.options.sshArgs.includes("BatchMode=yes"));
    assert.equal(session.forwards.length, 1, "the forward is reused");
    assert.equal(
      (fs.statSync(path.dirname(session.forwards[0][0])).mode & 0o777).toString(8),
      "700",
    );
  });

  it("forwards a restarted host's new socket", async () => {
    const restarted = "/tmp/omnigent-workspace-cd34/workspace.sock";
    await call(routes);
    const response = await call(routes, { socketPath: restarted });

    assert.equal(Buffer.from(response.body).toString(), `${restarted} Bearer t`);
    assert.equal(ssh.sessions[0].forwards.length, 2);
  });

  it("refuses hosts it didn't connect, and remote paths of another shape", async () => {
    const refused = {
      "an unrecorded host": { hostId: "host-2" },
      "no host": { hostId: undefined },
      "another socket name": { socketPath: "/tmp/omnigent-workspace-ab/other.sock" },
      "another directory": { socketPath: "/var/run/docker.sock" },
      "a relative path": { socketPath: "omnigent-workspace-ab/workspace.sock" },
      "an unnormalized path": { socketPath: "/tmp/../omnigent-workspace-ab/workspace.sock" },
      "a path ssh -L would misread": {
        socketPath: "/tmp/a:b/omnigent-workspace-ab/workspace.sock",
      },
    };
    await Promise.all(
      Object.entries(refused).map(([name, overrides]) =>
        assert.rejects(call(routes, overrides), /not a workspace socket/, name),
      ),
    );
    assert.equal(ssh.sessions.length, 0, "no connection is opened");
  });

  it("drops a failed connection, so the next read opens a new one", async () => {
    ssh = fakeSsh({ openError: "Permission denied" });
    forwards = createWorkspaceSshForwards({ createSession: ssh.createSession });
    routes.forwards = forwards;

    await assert.rejects(call(routes), /Permission denied/);
    await assert.rejects(call(routes), /Permission denied/);

    assert.equal(ssh.sessions.length, 2);
    assert.ok(ssh.sessions.every((session) => session.closed));
  });

  it("reports a refused forward", async () => {
    ssh = fakeSsh({ forwardCode: 255 });
    forwards = createWorkspaceSshForwards({ createSession: ssh.createSession });
    routes.forwards = forwards;

    await assert.rejects(call(routes), /couldn't forward the workspace socket on my-box/);
    assert.equal(ssh.sessions[0].closed, true, "a refused forward drops its connection");
  });

  it("keeps the shared connection when one request fails on its own", async () => {
    const oversized = "/tmp/omnigent-workspace-big/workspace.sock";
    ssh = fakeSsh({
      respond: (remotePath, req, res) => {
        if (remotePath === oversized) res.end(Buffer.alloc(16 * 1024 * 1024 + 1));
        else answer(remotePath, req, res);
      },
    });
    forwards = createWorkspaceSshForwards({ createSession: ssh.createSession });
    routes.forwards = forwards;

    await assert.rejects(call(routes, { socketPath: oversized }), /too large/);
    const response = await call(routes);

    assert.equal(Buffer.from(response.body).toString(), `${REMOTE} Bearer t`);
    assert.equal(ssh.sessions.length, 1);
    assert.equal(ssh.sessions[0].closed, false);
  });

  it("drops the connection when a forwarded socket refuses, so the next read opens a new one", async () => {
    const gone = "/tmp/omnigent-workspace-gone/workspace.sock";
    ssh = fakeSsh({
      respond: (remotePath, req, res) => {
        if (remotePath === gone) req.socket.destroy();
        else answer(remotePath, req, res);
      },
    });
    forwards = createWorkspaceSshForwards({ createSession: ssh.createSession });
    routes.forwards = forwards;

    await assert.rejects(call(routes, { socketPath: gone }), { code: "ECONNRESET" });
    assert.equal(ssh.sessions[0].closed, true);
    const response = await call(routes);

    assert.equal(Buffer.from(response.body).toString(), `${REMOTE} Bearer t`);
    assert.equal(ssh.sessions.length, 2);
  });

  it("a late failure leaves the connection that replaced its own", async () => {
    const stale = await forwards.localSocket("my-box", REMOTE);
    stale.dropConnection();
    await forwards.localSocket("my-box", REMOTE);

    stale.dropConnection();

    assert.equal(ssh.sessions[0].closed, true);
    assert.equal(ssh.sessions[1].closed, false, "the replacement stays open");
  });

  it("doesn't forward through a connection closed while it was opening", async () => {
    let openDone;
    ssh = fakeSsh({
      openGate: new Promise((resolve) => {
        openDone = resolve;
      }),
    });
    forwards = createWorkspaceSshForwards({ createSession: ssh.createSession });
    routes.forwards = forwards;

    const pending = call(routes);
    forwards.closeAll();
    openDone();

    await assert.rejects(pending, /the connection to my-box closed/);
    assert.equal(ssh.sessions[0].forwards.length, 0);
  });

  it("closeAll kills the connections and removes their directories at once", async () => {
    await call(routes);
    const [session] = ssh.sessions;
    const directory = path.dirname(session.forwards[0][0]);

    forwards.closeAll();

    assert.equal(session.aborted, true);
    assert.equal(fs.existsSync(directory), false);
  });

  it("closes an idle connection", async () => {
    forwards = createWorkspaceSshForwards({ createSession: ssh.createSession, idleMs: 10 });
    routes.forwards = forwards;
    await call(routes);

    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });

    assert.equal(ssh.sessions[0].closed, true);
  });
});

describe("createSshHostStore", () => {
  let directory;

  beforeEach(() => {
    directory = fs.mkdtempSync(path.join(os.tmpdir(), "ssh-hosts-"));
  });

  afterEach(() => {
    fs.rmSync(directory, { recursive: true, force: true });
  });

  it("keeps each server's hosts apart, across restarts, in a private file", () => {
    const file = path.join(directory, "ssh-hosts.json");
    const hostId = "0123456789abcdef0123456789abcdef";
    createSshHostStore(() => file).record("https://a.example/", hostId, "my-box");

    const reloaded = createSshHostStore(() => file);

    assert.equal(reloaded.destination("https://a.example/", hostId), "my-box");
    assert.equal(
      reloaded.destination("https://a.example/", "01234567-89ab-cdef-0123-456789ABCDEF"),
      "my-box",
      "a dashed id finds the same host",
    );
    assert.equal(reloaded.destination("https://b.example/", hostId), null);
    assert.equal(reloaded.destination("https://a.example/", "__proto__"), null);
    assert.equal((fs.statSync(file).mode & 0o777).toString(8), "600");
  });

  it("ignores a destination that isn't a valid ssh destination", () => {
    const file = path.join(directory, "ssh-hosts.json");
    fs.writeFileSync(file, JSON.stringify({ "https://a.example/": { h: "-oProxyCommand=x" } }));

    assert.equal(createSshHostStore(() => file).destination("https://a.example/", "h"), null);
  });
});
