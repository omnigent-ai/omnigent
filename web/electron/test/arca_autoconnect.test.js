"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { createArcaAutoConnect } = require("../src/arca_autoconnect");

const SERVER = "https://workspace.cloud.databricks.com/ml/omnigents";
const ORIGIN = "https://workspace.cloud.databricks.com";

/** A controllable startConnect: each call records itself and waits for finish(). */
function fakeConnects() {
  const runs = [];
  const startConnect = (serverUrl, onOutput) => {
    let resolve;
    const promise = new Promise((r) => {
      resolve = r;
    });
    const run = { serverUrl, onOutput, finish: (result) => resolve(result) };
    runs.push(run);
    return { command: "arca ssh …", promise, cancel: () => {} };
  };
  return { runs, startConnect };
}

function harness({ eligible = true } = {}) {
  const connects = fakeConnects();
  const events = [];
  const auto = createArcaAutoConnect({
    isEligible: () => eligible,
    startConnect: connects.startConnect,
    commandLine: () => "arca ssh isaac omni host",
    onStatus: (origin, status) => events.push({ origin, status }),
    now: () => 1000,
  });
  return { auto, events, runs: connects.runs };
}

describe("arca auto-connect", () => {
  it("does nothing for an ineligible server", async () => {
    const { auto, runs } = harness({ eligible: false });
    const status = await auto.ensure(SERVER);
    assert.equal(status.state, "unavailable");
    assert.equal(runs.length, 0);
  });

  it("runs once per origin and shares the in-flight run", async () => {
    const { auto, runs, events } = harness();
    const first = auto.ensure(SERVER);
    const second = auto.ensure(`${ORIGIN}/other`);
    assert.equal(runs.length, 1);
    assert.equal(auto.getStatus(SERVER).state, "starting");
    assert.equal(events[0].origin, ORIGIN);
    assert.equal(events[0].status.command, "arca ssh isaac omni host");

    runs[0].finish({ ok: true, alreadyRunning: false });
    const [a, b] = await Promise.all([first, second]);
    assert.equal(a.state, "online");
    assert.equal(b.state, "online");
    assert.equal(a.alreadyRunning, false);

    // A later window load in the same launch doesn't re-run.
    await auto.ensure(SERVER);
    assert.equal(runs.length, 1);
  });

  it("reports a warm launch as already running", async () => {
    const { auto, runs } = harness();
    const pending = auto.ensure(SERVER);
    runs[0].finish({ ok: true, alreadyRunning: true });
    const status = await pending;
    assert.equal(status.state, "online");
    assert.equal(status.alreadyRunning, true);
  });

  it("keeps the failure kind and output tail, and retries only after a failure", async () => {
    const { auto, runs } = harness();
    const pending = auto.ensure(SERVER);
    runs[0].onOutput("ssh: connecting…\n");
    assert.equal(auto.getStatus(SERVER).output, "ssh: connecting…\n");
    runs[0].finish({ ok: false, errorKind: "omni-auth", error: "not signed in" });
    const failed = await pending;
    assert.equal(failed.state, "failed");
    assert.equal(failed.errorKind, "omni-auth");
    assert.equal(failed.output, "ssh: connecting…\n");

    // ensure() after a failure doesn't nag with another run this launch.
    await auto.ensure(SERVER);
    assert.equal(runs.length, 1);

    const retry = auto.retry(SERVER);
    assert.equal(runs.length, 2);
    runs[1].finish({ ok: true });
    assert.equal((await retry).state, "online");

    // Retrying a healthy connection is refused.
    const again = await auto.retry(SERVER);
    assert.equal(again.state, "online");
    assert.equal(runs.length, 2);
  });

  it("shares an in-flight retry instead of starting another run", async () => {
    const { auto, runs } = harness();
    const pending = auto.ensure(SERVER);
    runs[0].finish({ ok: false, errorKind: "timeout", error: "timed out" });
    await pending;
    const first = auto.retry(SERVER);
    const second = auto.retry(SERVER);
    assert.equal(runs.length, 2);
    runs[1].finish({ ok: true });
    assert.equal((await first).state, "online");
    assert.equal((await second).state, "online");
  });

  it("exposes the in-flight run for the manual connect to share", async () => {
    const { auto, runs } = harness();
    assert.equal(auto.inFlight(SERVER), null);
    void auto.ensure(SERVER);
    const shared = auto.inFlight(SERVER);
    assert.ok(shared);
    runs[0].finish({ ok: true });
    assert.equal((await shared).state, "online");
    assert.equal(auto.inFlight(SERVER), null);
  });

  it("treats an unparseable server URL as unavailable", async () => {
    const { auto } = harness();
    assert.equal(auto.getStatus(null).state, "unavailable");
    assert.equal(auto.getStatus("not a url").state, "unavailable");
  });
});
