const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");

const { createWorkspaceSshForwards } = require("../src/workspaceSshForwards");
const { createSshTargetChecks, isConfiguredSshAlias } = require("../src/sshTargets");
const { workspaceSocketRequest } = require("../src/workspaceSocket");

const METHOD = "/omnigent.api.workspace.v1alpha1.WorkspaceService/ReadFile";
const PROBE = "/omnigent.api.workspace.v1alpha1.WorkspaceService/ListDirectory";
const REMOTE = "/tmp/omnigent-workspace-ab12/workspace.sock";
const unix = process.platform !== "win32";

/** A shared connection whose forwards answer like a host that accepts only `Bearer good`. */
function fakeSsh() {
  const sessions = [];
  const servers = [];
  const createSession = (options) => {
    const session = {
      options,
      closed: false,
      open: async () => {},
      forwardSocket: async (localPath) => {
        const server = http.createServer((req, res) => {
          session.paths = [...(session.paths ?? []), req.url];
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
    sessions.push(session);
    return session;
  };
  return { sessions, createSession, stop: () => servers.forEach((server) => server.close()) };
}

describe("an SSH alias the page suggests", { skip: !unix && "needs Unix sockets" }, () => {
  let ssh;
  let forwards;
  let recorded;
  let configured;
  let routes;

  beforeEach(() => {
    ssh = fakeSsh();
    forwards = createWorkspaceSshForwards({ createSession: ssh.createSession });
    recorded = [];
    configured = new Set(["my-box"]);
    const checks = createSshTargetChecks({
      record: (...entry) => recorded.push(entry),
      isConfigured: async (alias) => configured.has(alias),
    });
    routes = {
      sshDestination: () => null,
      verifySshTarget: (hostId, alias, proves) =>
        checks.verify("https://server.example.com/", hostId, alias, proves),
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

  it("is recorded once a token-checked call over it succeeds", async () => {
    const response = await call();

    assert.equal(response.status, 200);
    assert.deepEqual(recorded, [["https://server.example.com/", "host-1", "my-box"]]);
    assert.deepEqual(ssh.sessions[0].paths, [PROBE, METHOD]);
    assert.ok(ssh.sessions[0].options.sshArgs.includes("BatchMode=yes"));
  });

  it("is never tried unless the user's SSH config defines it", async () => {
    const refused = {
      "an alias the config doesn't define": { sshTarget: "other-box" },
      "a user@host destination": { sshTarget: "me@my-box" },
      "an option": { sshTarget: "-oProxyCommand=x" },
      "no host": { hostId: undefined },
    };
    await Promise.all(
      Object.entries(refused).map(([name, overrides]) =>
        assert.rejects(call(overrides), /not a workspace socket/, name),
      ),
    );
    assert.equal(ssh.sessions.length, 0, "no connection is opened");
    assert.deepEqual(recorded, []);
  });

  it("isn't recorded, or tried again soon, when the host refuses the token there", async () => {
    await assert.rejects(
      call({ headers: { authorization: "Bearer other" } }),
      /not a workspace socket/,
    );
    await assert.rejects(call(), /not a workspace socket/);

    assert.deepEqual(recorded, []);
    assert.equal(ssh.sessions.length, 1, "the second read doesn't connect again");
    assert.equal(ssh.sessions[0].closed, true, "the refused connection is closed");
  });

  it("is ignored when the host already has a recorded destination", async () => {
    routes.sshDestination = () => "recorded-box";

    await call({ sshTarget: "my-box" });

    assert.equal(ssh.sessions[0].options.destination, "recorded-box");
    assert.deepEqual(recorded, []);
  });
});

describe("isConfiguredSshAlias", () => {
  const configOutput = (stdout, error = null) => ({
    execFile: (_command, _args, _options, callback) => callback(error, stdout),
  });

  it("accepts an alias that resolves elsewhere or through a proxy, and nothing else", async () => {
    const cases = [
      ["hostname 10.0.0.5\nuser me\n", true],
      ["hostname box\nproxycommand ssh-proxy %h\n", true],
      ["hostname box\nproxyjump bastion\n", true],
      ["hostname box\nproxycommand none\n", false],
      ["hostname BOX\n", false],
      ["", false],
    ];
    const results = await Promise.all(
      cases.map(([stdout]) => isConfiguredSshAlias("box", configOutput(stdout))),
    );
    assert.deepEqual(
      results,
      cases.map(([, expected]) => expected),
    );
    assert.equal(
      await isConfiguredSshAlias("box", configOutput("", new Error("ssh missing"))),
      false,
    );
  });
});
