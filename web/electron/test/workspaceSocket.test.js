const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const { workspaceSocketRequest } = require("../src/workspaceSocket");

const METHOD = "/omnigent.api.workspace.v1alpha1.WorkspaceService/ReadFile";
const unix = process.platform !== "win32";

describe("workspaceSocketRequest", { skip: !unix && "needs Unix sockets" }, () => {
  let directory;
  let socketPath;
  let server;
  let received;
  let responseBody = Buffer.from([0, 0, 0, 0, 1, 0xff]);

  before(async () => {
    // Shaped like the host's: a 0700 omnigent-workspace-* directory.
    directory = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-workspace-"));
    socketPath = path.join(directory, "workspace.sock");
    server = http.createServer((req, res) => {
      const chunks = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", () => {
        received = { url: req.url, headers: req.headers, body: Buffer.concat(chunks) };
        res.writeHead(200, { "content-type": "application/connect+proto" });
        res.end(responseBody);
      });
    });
    await new Promise((resolve) => {
      server.listen(socketPath, resolve);
    });
  });

  after(() => {
    server.close();
    fs.rmSync(directory, { recursive: true, force: true });
  });

  const call = (overrides = {}) =>
    workspaceSocketRequest({
      socketPath,
      path: METHOD,
      headers: { authorization: "Bearer t", "content-type": "application/connect+proto" },
      body: new Uint8Array([0, 0, 0, 0, 0]),
      ...overrides,
    });

  it("forwards a workspace API call and returns the host's raw response", async () => {
    const response = await call({ headers: { authorization: "Bearer t", cookie: "secret=1" } });

    assert.equal(response.status, 200);
    assert.equal(response.headers["content-type"], "application/connect+proto");
    assert.deepEqual([...response.body], [0, 0, 0, 0, 1, 0xff]);
    assert.equal(received.url, METHOD);
    assert.equal(received.headers.authorization, "Bearer t");
    assert.equal(received.headers.cookie, undefined, "only API headers are forwarded");
    assert.deepEqual([...received.body], [0, 0, 0, 0, 0]);
  });

  it("refuses anything but a workspace API call on a workspace socket", async () => {
    const otherSocket = path.join(os.tmpdir(), "workspace.sock");
    const notASocket = /not a workspace socket/;
    const refused = {
      "another service's socket": [{ socketPath: "/var/run/docker.sock" }, notASocket],
      "a socket in another directory": [{ socketPath: otherSocket }, notASocket],
      "a missing socket": [
        {
          socketPath: path.join(path.dirname(directory), "omnigent-workspace-x", "workspace.sock"),
        },
        notASocket,
      ],
      "an unnormalized path": [{ socketPath: `${directory}/sub/../workspace.sock` }, notASocket],
      "a relative path": [{ socketPath: "omnigent-workspace-x/workspace.sock" }, notASocket],
      "another method path": [{ path: "/v1/sessions" }, /not a workspace API method/],
      "a path with a query": [{ path: `${METHOD}?x=1` }, /not a workspace API method/],
      "a non-binary body": [{ body: "{}" }, /invalid request body/],
    };
    await Promise.all(
      Object.entries(refused).map(([name, [overrides, reason]]) =>
        assert.rejects(call(overrides), reason, name),
      ),
    );
  });

  it("refuses a response larger than any capped read", async () => {
    responseBody = Buffer.alloc(16 * 1024 * 1024 + 1);
    try {
      await assert.rejects(call(), /workspace API response too large/);
    } finally {
      responseBody = Buffer.from([0, 0, 0, 0, 1, 0xff]);
    }
  });

  it("refuses a workspace socket other users can enter", async () => {
    fs.chmodSync(directory, 0o755);
    try {
      await assert.rejects(call(), /not a workspace socket/);
    } finally {
      fs.chmodSync(directory, 0o700);
    }
  });
});
