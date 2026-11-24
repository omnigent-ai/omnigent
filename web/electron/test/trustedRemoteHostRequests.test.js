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
    const requests = createTrustedRemoteHostRequests({
      isTrusted: () => false,
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
  });

  it("a failed persist does not grant trust", async () => {
    const requests = createTrustedRemoteHostRequests({
      isTrusted: () => false,
      add: () => false,
    });
    assert.equal(await requests.request(SERVER, HOST, async () => true), false);
    assert.equal(requests.hint(SERVER, HOST), null);
  });

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
    assert.equal(await requests.request(SERVER, "", approve), false);
    assert.equal(approvals, 0);
  });
});
