"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { createTrustedRemoteHostRequests } = require("../src/trustedRemoteHostRequests");

const SERVER = "https://server.example.com";
const HOST = "sandbox-host";

describe("trusted remote host requests", () => {
  it("asks once, persists on approval, and shares concurrent requests", async () => {
    let approvals = 0;
    let trusted = false;
    const requests = createTrustedRemoteHostRequests({
      isTrusted: () => trusted,
      add: () => {
        trusted = true;
        return true;
      },
    });
    const attempt = requests.request(SERVER, HOST, async () => {
      approvals += 1;
      return true;
    });
    assert.equal(await requests.request(SERVER, HOST, async () => false), await attempt);
    assert.equal(await attempt, true);
    assert.equal(approvals, 1);
    assert.equal(trusted, true);
    assert.equal(await requests.request(SERVER, HOST, async () => false), true);
  });

  it("declines hold until restart and surface in the hint", async () => {
    let trusted = false;
    const requests = createTrustedRemoteHostRequests({
      isTrusted: () => trusted,
      add: () => true,
    });
    let approvals = 0;
    assert.equal(
      await requests.request(SERVER, HOST, async () => {
        approvals += 1;
        return false;
      }),
      false,
    );
    assert.equal(
      await requests.request(SERVER, HOST, async () => {
        approvals += 1;
        return true;
      }),
      false,
    );
    assert.equal(approvals, 1);
    assert.match(requests.hint(SERVER, HOST), /restart Omnigent/);
    assert.equal(requests.hint(SERVER, "other-host"), null);
    trusted = true;
    assert.equal(
      await requests.request(SERVER, HOST, async () => {
        throw new Error("must not prompt");
      }),
      true,
    );
  });

  it("accepts trust added while the native dialog is open", async () => {
    let trusted = false;
    const approvalStarted = Promise.withResolvers();
    const consent = Promise.withResolvers();
    const requests = createTrustedRemoteHostRequests({
      isTrusted: () => trusted,
      add: () => false,
    });
    const pending = requests.request(SERVER, HOST, () => {
      approvalStarted.resolve();
      return consent.promise;
    });
    await approvalStarted.promise;
    trusted = true;
    consent.resolve(true);
    assert.equal(await pending, true);
  });

  for (const fault of ["cancelled", "dialog threw", "save failed", "save threw"]) {
    it(`allows another attempt after ${fault}`, async () => {
      let fail = true;
      let prompts = 0;
      const requests = createTrustedRemoteHostRequests({
        isTrusted: () => false,
        add: () => {
          if (fail && fault === "save threw") throw new Error("save failed");
          return !(fail && fault === "save failed");
        },
      });
      const approve = async () => {
        prompts++;
        if (fail && fault === "dialog threw") throw new Error("dialog closed");
        return fail && fault === "cancelled" ? null : true;
      };
      assert.equal(await requests.request(SERVER, HOST, approve), false);
      assert.equal(requests.hint(SERVER, HOST), null);
      fail = false;
      assert.equal(await requests.request(SERVER, HOST, approve), true);
      assert.equal(prompts, 2);
    });
  }

  it("refuses malformed server and host values without asking", async () => {
    const requests = createTrustedRemoteHostRequests({
      isTrusted: () => false,
      add: () => true,
    });
    let approvals = 0;
    const approve = async () => {
      approvals += 1;
      return true;
    };
    assert.equal(await requests.request("not a url", HOST, approve), false);
    await Promise.all(
      ["", "existing.host", "line\nbreak", "name\u202e", "a".repeat(65)].map(async (invalid) => {
        assert.equal(await requests.request(SERVER, invalid, approve), false);
      }),
    );
    assert.equal(approvals, 0);
  });
});
