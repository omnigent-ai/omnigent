// How suggested aliases are asked about and remembered. The reads over them are
// covered in workspaceSshForwards.test.js.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { createSshTargetChecks } = require("../src/sshTargets");

const SERVER = "https://server.example.com/";

describe("createSshTargetChecks", () => {
  it("asks again when the question couldn't be shown, but not after Not now", async () => {
    const checks = createSshTargetChecks({ record: () => {} });
    let asked = 0;
    const verify = (answer) =>
      checks.verify(SERVER, "host-1", "my-box", {
        approve: async () => {
          asked += 1;
          return answer();
        },
        proves: async () => true,
      });

    assert.equal(await verify(() => null), false);
    assert.equal(
      await verify(() => {
        throw new Error("no window");
      }),
      false,
    );
    assert.equal(await verify(() => false), false);
    assert.equal(await verify(() => true), false);
    assert.equal(asked, 3);
  });

  it("keeps a proven alias, without probing again, when recording it fails", async (t) => {
    t.mock.method(console, "warn", () => {});
    const checks = createSshTargetChecks({
      record: () => {
        throw new Error("disk full");
      },
    });
    let probes = 0;
    const steps = {
      approve: async () => true,
      proves: async () => {
        probes += 1;
        return true;
      },
    };

    assert.equal(await checks.verify(SERVER, "host-1", "my-box", steps), true);
    assert.equal(await checks.verify(SERVER, "host-1", "my-box", steps), true);
    assert.equal(probes, 1);
  });
});
