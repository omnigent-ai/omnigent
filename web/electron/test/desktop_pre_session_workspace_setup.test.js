"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { once } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  startPodServices,
  startWorkspaceFixtures,
} = require("../e2e/desktop_pre_session_workspace_setup");

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

describe("isolated pod service setup", () => {
  for (const stage of ["mock readiness", "mock fallback", "pod readiness"]) {
    it(`releases processes and logs when ${stage} fails`, async (t) => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), "omni-pod-setup-"));
      const processes = [];
      const descriptors = [];
      const originalOpen = fs.openSync;
      t.mock.method(fs, "openSync", (...args) => {
        const descriptor = originalOpen(...args);
        descriptors.push(descriptor);
        return descriptor;
      });
      t.after(() => {
        for (const proc of processes) {
          if (proc.exitCode === null && proc.signalCode === null) proc.kill("SIGKILL");
        }
        fs.rmSync(root, { recursive: true, force: true });
      });
      const service = (name) => ({
        command: process.execPath,
        args: ["-e", "setInterval(() => {}, 1000)"],
        logPath: path.join(root, `${name}.log`),
      });
      const failure = new Error(stage);
      let fallbackReached = false;
      await assert.rejects(
        startPodServices({
          mock: service("mock"),
          pod: service("pod"),
          waitForMock: async (proc) => {
            processes.push(proc);
            await once(proc, "spawn");
            if (stage === "mock readiness") throw failure;
          },
          configureMock: async () => {
            fallbackReached = true;
            if (stage === "mock fallback") throw failure;
          },
          waitForPod: async (proc) => {
            processes.push(proc);
            await once(proc, "spawn");
            throw failure;
          },
        }),
        (error) => error === failure,
      );
      assert.equal(fallbackReached, stage !== "mock readiness");
      assert.equal(processes.length, stage === "pod readiness" ? 2 : 1);
      for (const proc of processes) {
        assert.throws(() => process.kill(proc.pid, 0), { code: "ESRCH" });
      }
      for (const descriptor of descriptors) {
        assert.throws(() => fs.fstatSync(descriptor), { code: "EBADF" });
      }
    });
  }

  it("transfers successful setup to an idempotent close handle", async (t) => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omni-pod-setup-"));
    const processes = [];
    t.after(() => {
      for (const proc of processes) {
        if (proc.exitCode === null && proc.signalCode === null) proc.kill("SIGKILL");
      }
      fs.rmSync(root, { recursive: true, force: true });
    });
    const service = (name) => ({
      command: process.execPath,
      args: ["-e", "setInterval(() => {}, 1000)"],
      logPath: path.join(root, `${name}.log`),
    });
    const hosts = [{ host_id: "host-test", status: "online" }];
    const result = await startPodServices({
      mock: service("mock"),
      pod: service("pod"),
      waitForMock: async (proc) => {
        processes.push(proc);
        await once(proc, "spawn");
      },
      configureMock: async () => {},
      waitForPod: async (proc) => {
        processes.push(proc);
        await once(proc, "spawn");
        return hosts;
      },
    });
    assert.deepEqual(result.hosts, hosts);
    for (const proc of processes) assert.equal(process.kill(proc.pid, 0), true);
    await result.close();
    await result.close();
    for (const proc of processes) {
      assert.throws(() => process.kill(proc.pid, 0), { code: "ESRCH" });
    }
  });
});
