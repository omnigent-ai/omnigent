const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { isHostThisMachine } = require("../src/hostIdentity");

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
