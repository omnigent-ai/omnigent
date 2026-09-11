"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { startWorkspaceFixtures } = require("../e2e/desktop_pre_session_workspace_setup");

describe("pre-session workspace fixture setup", () => {
  it("closes a started pod when page setup fails", async () => {
    let closed = false;
    const failure = new Error("page setup failed");

    await assert.rejects(
      startWorkspaceFixtures(
        async () => ({
          close: async () => {
            closed = true;
          },
        }),
        async () => {
          throw failure;
        },
      ),
      (error) => error === failure,
    );

    assert.equal(closed, true);
  });

  it("does not start the page when pod setup fails", async () => {
    let pageStarted = false;
    const failure = new Error("pod setup failed");

    await assert.rejects(
      startWorkspaceFixtures(
        async () => {
          throw failure;
        },
        async () => {
          pageStarted = true;
          return { close: async () => {} };
        },
      ),
      (error) => error === failure,
    );

    assert.equal(pageStarted, false);
  });
});
