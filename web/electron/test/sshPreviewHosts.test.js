"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { createSshPreviewHosts } = require("../src/sshPreviewHosts");

const SERVER = "https://server.example/";
const HOST = "a".repeat(32);

/** Preview hosts over an in-memory destination record, keyed like sshHosts. */
function setup(records = { [SERVER]: { [HOST]: "devbox" } }) {
  const destinations = structuredClone(records);
  const previews = createSshPreviewHosts({
    destination: (serverUrl, hostId) =>
      destinations[serverUrl]?.[hostId.replace(/-/g, "").toLowerCase()] ?? null,
  });
  return { destinations, previews };
}

describe("localhost previews for hosts reached over SSH", () => {
  it("verifies an unrecorded suggestion after consent, then records its preview proof", async () => {
    let destination = null;
    const steps = [];
    const consent = Promise.withResolvers();
    const asked = Promise.withResolvers();
    const previews = createSshPreviewHosts({
      destination: () => destination,
      verify: async (server, host, target) => {
        steps.push(["verify", server, host, target]);
        return true;
      },
      record: (server, host, target) => {
        steps.push(["record", server, host, target]);
        destination = target;
      },
    });
    const pending = previews.request(
      SERVER,
      HOST,
      async (target, needsVerification) => {
        steps.push(["approve", target, needsVerification]);
        asked.resolve();
        return consent.promise;
      },
      "devbox",
    );
    await asked.promise;
    assert.deepEqual(steps, [["approve", "devbox", true]], "SSH must wait for the answer");
    assert.equal(previews.allows(SERVER, HOST), false);
    consent.resolve(true);
    assert.equal(await pending, true);
    assert.deepEqual(steps, [
      ["approve", "devbox", true],
      ["verify", SERVER, HOST, "devbox"],
      ["record", SERVER, HOST, "devbox"],
    ]);
    assert.equal(previews.allows(SERVER, HOST), true);
  });

  it("never probes a declined suggestion or grants a failed proof", async () => {
    let attempts = 0;
    let questions = 0;
    let records = 0;
    const previews = createSshPreviewHosts({
      destination: () => null,
      verify: async () => {
        attempts++;
        return false;
      },
      record: () => records++,
    });
    assert.equal(await previews.request(SERVER, HOST, async () => false, "devbox"), false);
    assert.equal(await previews.request(SERVER, HOST, async () => true, "devbox"), false);
    assert.equal(attempts, 0);
    assert.equal(await previews.request(SERVER, "b".repeat(32), async () => true, "devbox"), false);
    assert.equal(attempts, 1);
    assert.equal(previews.allows(SERVER, "b".repeat(32)), false);
    assert.match(previews.hint(SERVER, "b".repeat(32)), /could not be verified/);
    assert.equal(
      await previews.request(
        SERVER,
        "b".repeat(32),
        async () => {
          questions++;
          return true;
        },
        "devbox",
      ),
      false,
    );
    assert.equal(questions, 0);
    assert.equal(records, 0);
    assert.equal(attempts, 1);
  });

  for (const failure of ["verification", "storage"]) {
    it(`fails closed without repeating consent when ${failure} throws`, async (t) => {
      let destination = "devbox";
      let questions = 0;
      let proofs = 0;
      let records = 0;
      const warnings = t.mock.method(console, "warn", () => {});
      const previews = createSshPreviewHosts({
        destination: () => destination,
        verify: async () => {
          proofs++;
          if (failure === "verification") throw new Error("offline");
          return true;
        },
        record: () => {
          records++;
          throw new Error("disk full");
        },
      });
      const approve = async () => {
        questions++;
        return true;
      };
      assert.equal(await previews.request(SERVER, HOST, async () => true), true);
      destination = null;
      assert.equal(await previews.request(SERVER, HOST, approve, "devbox"), false);
      assert.equal(await previews.request(SERVER, HOST, approve, "another-alias"), false);
      destination = "devbox";
      assert.equal(previews.allows(SERVER, HOST), false);
      assert.equal(await previews.request(SERVER, HOST, approve), false);
      assert.equal(questions, 1);
      assert.equal(proofs, 1);
      assert.equal(records, failure === "storage" ? 1 : 0);
      assert.equal(warnings.mock.callCount(), failure === "storage" ? 1 : 0);
      assert.match(previews.hint(SERVER, HOST), /restart Omnigent/);
    });
  }

  it("doesn't overwrite a destination recorded during verification", async () => {
    await Promise.all(
      [false, true].map(async (restored) => {
        let destination = null;
        let records = 0;
        const previews = createSshPreviewHosts({
          destination: () => destination,
          verify: async () => {
            destination = "replacement";
            previews.destinationChanged(SERVER, HOST);
            if (restored) {
              destination = "devbox";
              previews.destinationChanged(SERVER, HOST);
            }
            return true;
          },
          record: () => records++,
        });
        assert.equal(await previews.request(SERVER, HOST, async () => true, "devbox"), false);
        assert.equal(records, 0);
        assert.equal(destination, restored ? "devbox" : "replacement");
        assert.equal(previews.allows(SERVER, HOST), false);
      }),
    );
  });

  it("allows a recorded host only after the user allows it", async () => {
    const { previews } = setup();
    const asked = [];
    assert.equal(previews.allows(SERVER, HOST), false);
    assert.equal(
      await previews.request(SERVER, HOST, async (destination) => {
        asked.push(destination);
        return true;
      }),
      true,
    );
    assert.deepEqual(asked, ["devbox"]);
    assert.equal(previews.allows(SERVER, HOST), true);
    // The server may send the dashed form of the same host ID.
    assert.equal(previews.allows(SERVER, `${"a".repeat(8)}-${"a".repeat(24)}`.toUpperCase()), true);
    assert.equal(
      await previews.request(SERVER, HOST, () => assert.fail("must not ask again")),
      true,
    );
  });

  it("never asks about a host without a recorded destination, or for another server", async () => {
    const { previews } = setup();
    let questions = 0;
    const ask = async () => {
      questions++;
      return true;
    };
    assert.equal(await previews.request(SERVER, "b".repeat(32), ask), false);
    assert.equal(await previews.request("https://other.example/", HOST, ask), false);
    assert.equal(await previews.request(SERVER, null, ask), false);
    assert.equal(await previews.request(null, HOST, ask), false);
    assert.equal(previews.allows(SERVER, undefined), false);
    assert.equal(questions, 0);
  });

  it("treats a lookup without a destination string as unrecorded", async () => {
    await Promise.all(
      [undefined, "", 0].map(async (missing) => {
        let questions = 0;
        const previews = createSshPreviewHosts({ destination: () => missing });
        assert.equal(previews.allows(SERVER, HOST), false);
        assert.equal(
          await previews.request(SERVER, HOST, async () => {
            questions++;
            return true;
          }),
          false,
        );
        assert.equal(questions, 0);
      }),
    );
  });

  it("keeps a declined host blocked until restart and explains why", async () => {
    const { destinations, previews } = setup();
    let questions = 0;
    const decline = async () => {
      questions++;
      return false;
    };
    assert.equal(previews.hint(SERVER, HOST), null);
    assert.equal(await previews.request(SERVER, HOST, async () => true), true);
    destinations[SERVER][HOST] = "replacement";
    assert.equal(await previews.request(SERVER, HOST, decline), false);
    assert.equal(await previews.request(SERVER, HOST, decline), false);
    assert.equal(questions, 1);
    assert.equal(previews.allows(SERVER, HOST), false);
    assert.match(previews.hint(SERVER, HOST), /"replacement" were declined/);
    destinations[SERVER][HOST] = "devbox";
    assert.equal(previews.allows(SERVER, HOST), false);
    assert.equal(await previews.request(SERVER, HOST, decline), false);
    assert.match(previews.hint(SERVER, HOST), /"devbox" were declined/);
    assert.equal(questions, 1);
  });

  it("asks again after a question that couldn't be shown or failed", async () => {
    const { previews } = setup();
    assert.equal(await previews.request(SERVER, HOST, async () => null), false);
    assert.equal(
      await previews.request(SERVER, HOST, async () => {
        throw new Error("no window");
      }),
      false,
    );
    assert.equal(previews.hint(SERVER, HOST), null);
    assert.equal(await previews.request(SERVER, HOST, async () => true), true);
  });

  it("shares one question between concurrent requests", async () => {
    const { previews } = setup();
    const answer = Promise.withResolvers();
    let questions = 0;
    const ask = () => {
      questions++;
      return answer.promise;
    };
    const first = previews.request(SERVER, HOST, ask);
    const second = previews.request(SERVER, HOST, ask);
    answer.resolve(true);
    assert.deepEqual(await Promise.all([first, second]), [true, true]);
    assert.equal(questions, 1);
  });

  it("stops allowing a host whose recorded destination changed", async () => {
    const { destinations, previews } = setup();
    assert.equal(await previews.request(SERVER, HOST, async () => true), true);
    destinations[SERVER][HOST] = "replacement";
    assert.equal(previews.allows(SERVER, HOST), false);
    destinations[SERVER][HOST] = "devbox";
    assert.equal(
      previews.allows(SERVER, HOST),
      false,
      "restoring a destination must not revive consent",
    );
    assert.equal(await previews.request(SERVER, HOST, async () => true), true);
    destinations[SERVER][HOST] = null;
    assert.equal(previews.allows(SERVER, HOST), false);
    destinations[SERVER][HOST] = "devbox";
    assert.equal(previews.allows(SERVER, HOST), false);
  });

  it("doesn't apply an answer to a destination that changed while it was asked", async () => {
    const { destinations, previews } = setup();
    const result = await previews.request(SERVER, HOST, async () => {
      destinations[SERVER][HOST] = "replacement";
      return true;
    });
    assert.equal(result, false);
    assert.equal(previews.allows(SERVER, HOST), false);
  });
});
