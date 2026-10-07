"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  arcaTarget,
  readArcaIdentity,
  createArcaIdentityStore,
  isArcaAgentContext,
  IDENTITY_START,
  IDENTITY_END,
} = require("../src/arcaIdentity");

const SERVER = "https://account.databricks.com/omnigent?o=123";
const HOST = "a".repeat(32);
const identity = { serverUrl: arcaTarget(SERVER), hostId: HOST };
const daemon = {
  target: "https://account.databricks.com/api/2.0/omnigent",
  server_url: "https://account.databricks.com/api/2.0/omnigent",
  mode: "server",
  process: "online",
  host_status: "online",
  host_id: HOST,
  error: null,
};
const output = (rows) =>
  `connect output\n${IDENTITY_START}\n${JSON.stringify({ daemons: rows })}\n${IDENTITY_END}\n`;

describe("Arca daemon identity", () => {
  it("captures the live remote daemon and binds the selected workspace", () => {
    assert.deepEqual(readArcaIdentity(output([daemon]), SERVER), identity);
    assert.notEqual(arcaTarget(SERVER), arcaTarget(SERVER.replace("123", "456")));
    assert.equal(
      arcaTarget("https://account.databricks.com/api/2.0/omnigent/?o=123"),
      arcaTarget(SERVER),
    );
  });

  it("rejects unknown, ambiguous, mismatched, offline and failed status", () => {
    for (const rows of [
      [],
      [daemon, daemon],
      [{ ...daemon, target: "https://other.databricks.com/api/2.0/omnigent" }],
      [{ ...daemon, server_url: "https://other.databricks.com/api/2.0/omnigent" }],
      [{ ...daemon, process: "offline" }],
      [{ ...daemon, host_status: "offline" }],
      [{ ...daemon, error: "not authenticated" }],
      [{ ...daemon, mode: "local" }],
      [{ ...daemon, host_id: "arca-looking-hostname" }],
    ]) {
      assert.equal(readArcaIdentity(output(rows), SERVER), null);
    }
    for (const text of [
      "Host daemon already running",
      JSON.stringify({ daemons: [daemon] }),
      `${IDENTITY_START}bad JSON${IDENTITY_END}`,
    ]) {
      assert.equal(readArcaIdentity(text, SERVER), null);
    }
  });

  it("does not let a stale connect restore identity after a newer unknown or failed run", () => {
    const store = createArcaIdentityStore();
    const old = store.begin(SERVER);
    const current = store.begin(SERVER);
    old({ ok: true, identity });
    assert.equal(store.get(SERVER), null);
    current({ ok: true, identity });
    assert.deepEqual(store.get(SERVER), identity);
    const failed = store.begin(SERVER);
    assert.equal(store.get(SERVER), null);
    failed({ ok: false, identity });
    assert.equal(store.get(SERVER), null);
    store.begin(SERVER)({ ok: true });
    assert.equal(store.get(SERVER), null);
  });

  it("isolates workspace targets and refuses a result belonging to another target", () => {
    const store = createArcaIdentityStore();
    store.begin(SERVER)({ ok: true, identity });
    const other = SERVER.replace("123", "456");
    store.begin(other)({ ok: true, identity });
    assert.equal(store.get(other), null);
    assert.deepEqual(store.get(SERVER), identity);
  });

  it("requires main-owned identity, actual source host, and live feature/server gates", () => {
    const context = { serverTarget: arcaTarget(SERVER), sourceHostId: HOST };
    const gates = { enabled: true, managed: true, serverTarget: SERVER };
    assert.equal(isArcaAgentContext(identity, context, gates), true);
    for (const [id, ctx, gate] of [
      [null, context, gates],
      [identity, null, gates],
      [identity, { ...context, sourceHostId: null }, gates],
      [identity, { ...context, sourceHostId: "b".repeat(32) }, gates],
      [identity, { ...context, serverTarget: arcaTarget(SERVER.replace("123", "456")) }, gates],
      [identity, context, { ...gates, enabled: false }],
      [identity, context, { ...gates, managed: false }],
      [identity, context, { ...gates, serverTarget: SERVER.replace("123", "456") }],
    ]) {
      assert.equal(isArcaAgentContext(id, ctx, gate), false);
    }
  });
});
