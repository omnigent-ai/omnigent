const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { performance } = require("node:perf_hooks");
const {
  createHostThisMachineChecker,
  isHostThisMachine,
  readRemoteHostIdentity,
  IDENTITY_START,
  IDENTITY_END,
} = require("../src/hostIdentity");
const { normalizeServerTarget } = require("../src/url");

it("imports browser IPC and checks injected identities without desktop js-yaml", () => {
  execFileSync(process.execPath, [
    "-e",
    `const assert = require("node:assert/strict");
     const Module = require("node:module");
     const load = Module._load;
     Module._load = function (name, ...args) {
       assert.notEqual(name, "js-yaml", "Browser IPC must not load desktop js-yaml");
       return load.call(this, name, ...args);
     };
     const { isHostThisMachine, SESSION_HOST_PROTOCOL } = require(process.argv[1]);
     require(process.argv[2]);
     assert.equal(SESSION_HOST_PROTOCOL, 1);
     assert.equal(isHostThisMachine(
       { hostId: "local", serverUrl: "https://server.example" },
       { readLocalHostId: () => "local", liveDaemonRecordFor: () => ({ host_id: "local" }) },
     ), true);`,
    require.resolve("../src/hostIdentity"),
    require.resolve("../src/browserIpc"),
  ]);
});

describe("isHostThisMachine", () => {
  for (const { name, hostId, localId, record, expected } of [
    {
      name: "matching identity and live enrollment",
      hostId: "local",
      localId: "local",
      record: { host_id: "local" },
      expected: true,
    },
    {
      name: "legacy prefixed record identity",
      hostId: "local",
      localId: "local",
      record: { host_id: "host_local" },
      expected: true,
    },
    {
      name: "different session host",
      hostId: "remote",
      localId: "local",
      record: { host_id: "local" },
      expected: false,
    },
    {
      name: "no live enrollment",
      hostId: "local",
      localId: "local",
      record: null,
      expected: false,
    },
    {
      name: "record for another identity",
      hostId: "local",
      localId: "local",
      record: { host_id: "old" },
      expected: false,
    },
    {
      name: "legacy record without identity",
      hostId: "local",
      localId: "local",
      record: {},
      expected: true,
    },
    { name: "null session host", hostId: null, localId: "local", record: {}, expected: false },
    { name: "empty session host", hostId: "", localId: "local", record: {}, expected: false },
    { name: "no local identity", hostId: "local", localId: null, record: {}, expected: false },
  ]) {
    it(name, () => {
      let enrollmentReads = 0;
      const cli = {
        readLocalHostId: () => localId,
        liveDaemonRecordFor: (serverUrl) => {
          enrollmentReads++;
          assert.equal(serverUrl, "https://server.example");
          return record;
        },
      };
      assert.equal(
        isHostThisMachine({ hostId, serverUrl: "https://server.example" }, cli),
        expected,
      );
      assert.equal(enrollmentReads, hostId && hostId === localId ? 1 : 0);
    });
  }
});

describe("cached host eligibility", () => {
  it("expires grants with the production clock despite a backward wall-clock correction", (t) => {
    let elapsed = 0;
    let wallTime = 60_000;
    let live = true;
    let enrollmentReads = 0;
    t.mock.method(performance, "now", () => elapsed);
    t.mock.method(Date, "now", () => wallTime);
    const check = createHostThisMachineChecker({
      cli: {
        readLocalHostId: () => "local",
        liveDaemonRecordFor: () => {
          enrollmentReads++;
          return live ? { host_id: "local" } : null;
        },
      },
    });
    const context = { hostId: "local", serverUrl: "https://server.example" };
    assert.equal(check(context), true);
    live = false;
    wallTime -= 60_000;
    elapsed = 999;
    assert.equal(check(context), true);
    assert.equal(enrollmentReads, 1);
    elapsed = 1001;
    assert.equal(check(context), false);
    assert.equal(enrollmentReads, 2);
  });

  it("reuses reads within one second, isolates host/server pairs, and expires grants and denials", () => {
    let now = 0;
    let localId = "local";
    let live = true;
    let identityReads = 0;
    let enrollmentReads = 0;
    const check = createHostThisMachineChecker({
      now: () => now,
      cli: {
        readLocalHostId: () => {
          identityReads++;
          return localId;
        },
        liveDaemonRecordFor: () => {
          enrollmentReads++;
          return live ? { host_id: localId } : null;
        },
      },
    });
    const context = { hostId: "local", serverUrl: "https://server.example" };
    assert.equal(check(context), true);
    assert.equal(check(context), true);
    now = 999;
    live = false;
    assert.equal(check(context), true);
    assert.equal(identityReads, 1);
    assert.equal(enrollmentReads, 1);
    assert.equal(check({ ...context, serverUrl: "https://other.example" }), false);
    assert.equal(check({ ...context, hostId: "remote" }), false);
    assert.equal(identityReads, 3);
    assert.equal(enrollmentReads, 2);
    now = 1000;
    assert.equal(check(context), false);
    assert.equal(identityReads, 4);
    assert.equal(enrollmentReads, 3);
    live = true;
    now = 1999;
    assert.equal(check(context), false);
    now = 2000;
    assert.equal(check(context), true);
    localId = "changed";
    now = 3000;
    assert.equal(check(context), false);
    assert.equal(identityReads, 6);
    assert.equal(enrollmentReads, 4);
    assert.equal(check({ ...context, hostId: null }), false);
    assert.equal(identityReads, 6);
  });
});

describe("remote daemon identity", () => {
  const SERVER = "https://accounts.cloud.databricks.com/omnigent?o=123";
  const HOST = "a".repeat(32);
  const identity = { serverUrl: normalizeServerTarget(SERVER), hostId: HOST };
  const daemon = {
    target: "https://accounts.cloud.databricks.com/api/2.0/omnigent",
    server_url: "https://accounts.cloud.databricks.com/api/2.0/omnigent",
    mode: "server",
    process: "online",
    host_status: "online",
    host_id: HOST,
    error: null,
  };
  const output = (rows) =>
    `connect output\n${IDENTITY_START}\n${JSON.stringify({ daemons: rows })}\n${IDENTITY_END}\n`;

  it("captures the live remote daemon and binds the selected workspace", () => {
    assert.deepEqual(readRemoteHostIdentity(output([daemon]), SERVER), identity);
    assert.notEqual(
      normalizeServerTarget(SERVER),
      normalizeServerTarget(SERVER.replace("123", "456")),
    );
    assert.equal(
      normalizeServerTarget("https://accounts.cloud.databricks.com/api/2.0/omnigent/?o=123"),
      normalizeServerTarget(SERVER),
    );
  });

  it("accepts a daemon the CLI matched to the selected workspace through another URL", () => {
    const workspaceHost = "https://workspace.example.test/api/2.0/omnigent";
    const sameWorkspace = {
      ...daemon,
      target: workspaceHost,
      server_url: workspaceHost,
      same_workspace_as: daemon.target,
    };
    assert.deepEqual(readRemoteHostIdentity(output([sameWorkspace]), SERVER), identity);
  });

  it("accepts a daemon target with the requested or a sign-in-resolved ?o=, never another one", () => {
    const withOrg = (org) => ({ ...daemon, target: `${daemon.target}?o=${org}` });
    assert.deepEqual(readRemoteHostIdentity(output([withOrg("123")]), SERVER), identity);
    const unscoped = "https://accounts.cloud.databricks.com/omnigent";
    assert.deepEqual(readRemoteHostIdentity(output([withOrg("999")]), unscoped), {
      serverUrl: normalizeServerTarget(unscoped),
      hostId: HOST,
    });
    assert.equal(readRemoteHostIdentity(output([withOrg("456")]), SERVER), null);
  });

  it("rejects unknown, ambiguous, mismatched, offline and failed status", () => {
    for (const rows of [
      [],
      [daemon, daemon],
      [{ ...daemon, target: "https://other.example.test/api/2.0/omnigent" }],
      [
        {
          ...daemon,
          target: "https://other.example.test/api/2.0/omnigent",
          same_workspace_as: "https://another.example.test/api/2.0/omnigent",
        },
      ],
      [{ ...daemon, server_url: "https://other.example.test/api/2.0/omnigent" }],
      [{ ...daemon, process: "offline" }],
      [{ ...daemon, host_status: "offline" }],
      [{ ...daemon, error: "not authenticated" }],
      [{ ...daemon, mode: "local" }],
      [{ ...daemon, host_id: "hostname-without-id" }],
    ]) {
      assert.equal(readRemoteHostIdentity(output(rows), SERVER), null);
    }
    for (const text of [
      "Host daemon already running",
      JSON.stringify({ daemons: [daemon] }),
      `${IDENTITY_START}bad JSON${IDENTITY_END}`,
    ]) {
      assert.equal(readRemoteHostIdentity(text, SERVER), null);
    }
  });

  it("reports fixed, bounded reasons without disclosing status fields or output", () => {
    const privateText = "https://private.example/token-secret";
    for (const [text, reason] of [
      [privateText, "status markers missing"],
      [`${IDENTITY_START}${privateText}${IDENTITY_END}`, "status could not be parsed"],
      [output([]), "status must contain exactly one daemon"],
      [output([{ ...daemon, error: privateText }]), "daemon status or target not eligible"],
    ]) {
      const reasons = [];
      assert.equal(
        readRemoteHostIdentity(text, SERVER, (r) => reasons.push(r)),
        null,
      );
      assert.deepEqual(reasons, [reason]);
      assert.ok(reason.length < 80);
    }
    const reasons = [];
    assert.deepEqual(
      readRemoteHostIdentity(output([daemon]), SERVER, (r) => reasons.push(r)),
      identity,
    );
    assert.deepEqual(reasons, []);
  });
});
