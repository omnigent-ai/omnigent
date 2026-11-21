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
  it("verifies an unrecorded suggestion only after consent, then records and allows it", async () => {
    let destination = null;
    const steps = [];
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
    assert.equal(
      await previews.request(
        SERVER,
        HOST,
        async (target, needsVerification) => {
          steps.push(["approve", target, needsVerification]);
          return true;
        },
        "devbox",
      ),
      true,
    );
    assert.deepEqual(steps, [
      ["approve", "devbox", true],
      ["verify", SERVER, HOST, "devbox"],
      ["record", SERVER, HOST, "devbox"],
    ]);
    assert.equal(previews.allows(SERVER, HOST), true);
  });

  it("never probes a declined suggestion or grants a failed proof", async () => {
    let attempts = 0;
    const previews = createSshPreviewHosts({
      destination: () => null,
      verify: async () => {
        attempts++;
        return false;
      },
      record: () => assert.fail("must not record an unverified host"),
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
        () => assert.fail("must not repeatedly ask"),
        "devbox",
      ),
      false,
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
    const ask = () => assert.fail("must not ask");
    assert.equal(await previews.request(SERVER, "b".repeat(32), ask), false);
    assert.equal(await previews.request("https://other.example/", HOST, ask), false);
    assert.equal(await previews.request(SERVER, null, ask), false);
    assert.equal(await previews.request(null, HOST, ask), false);
    assert.equal(previews.allows(SERVER, undefined), false);
  });

  it("treats a lookup without a destination string as unrecorded", async () => {
    await Promise.all(
      [undefined, "", 0].map(async (missing) => {
        const previews = createSshPreviewHosts({ destination: () => missing });
        assert.equal(previews.allows(SERVER, HOST), false);
        assert.equal(
          await previews.request(SERVER, HOST, () => assert.fail("must not ask")),
          false,
        );
      }),
    );
  });

  it("keeps a declined host blocked until restart and explains why", async () => {
    const { previews } = setup();
    let questions = 0;
    const decline = async () => {
      questions++;
      return false;
    };
    assert.equal(previews.hint(SERVER, HOST), null);
    assert.equal(await previews.request(SERVER, HOST, decline), false);
    assert.equal(await previews.request(SERVER, HOST, decline), false);
    assert.equal(questions, 1);
    assert.equal(previews.allows(SERVER, HOST), false);
    assert.match(previews.hint(SERVER, HOST), /"devbox" were declined/);
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
    destinations[SERVER][HOST] = null;
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
