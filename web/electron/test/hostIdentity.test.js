const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { isHostThisMachine } = require("../src/hostIdentity");

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
      const cli = {
        readLocalHostId: () => localId,
        liveDaemonRecordFor: (serverUrl) => {
          assert.equal(serverUrl, "https://server.example");
          return record;
        },
      };
      assert.equal(
        isHostThisMachine({ hostId, serverUrl: "https://server.example" }, cli),
        expected,
      );
    });
  }
});
