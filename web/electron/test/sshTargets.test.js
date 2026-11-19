// How suggested aliases are asked about and remembered. The reads over them are
// covered in workspaceSshForwards.test.js.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { createSshTargetChecks } = require("../src/sshTargets");

const SERVER = "https://server.example.com/";
const RETRY_MS = 5 * 60_000;
const OTHER_SERVER = "https://other.example.com/";

/** Steps whose question resolves `answer` and whose probe resolves `proof`, counting both. */
function counted(answer, proof = true) {
  const steps = {
    asked: 0,
    probes: 0,
    approve: async () => {
      steps.asked += 1;
      return answer();
    },
    proves: async () => {
      steps.probes += 1;
      return proof;
    },
  };
  return steps;
}

describe("createSshTargetChecks", () => {
  it("asks once for a host however its id is written", async () => {
    const checks = createSshTargetChecks({ record: () => {} });
    const steps = counted(() => true);

    assert.equal(await checks.verify(SERVER, "AB-12", "my-box", steps), true);
    assert.equal(await checks.verify(SERVER, "ab12", "my-box", steps), true);
    assert.equal(steps.asked, 1);
    assert.equal(steps.probes, 1);
  });

  it("asks a server nothing more after Not now, whatever the alias", async () => {
    const checks = createSshTargetChecks({ record: () => {} });
    const declined = counted(() => false);
    const other = counted(() => true);

    assert.equal(await checks.verify(SERVER, "host-1", "a1", declined), false);
    assert.equal(await checks.verify(SERVER, "host-2", "a2", other), false);
    assert.equal(await checks.verify(OTHER_SERVER, "host-1", "a1", other), true);
    assert.equal(declined.asked, 1);
    assert.equal(other.asked, 1, "only the other server is asked");
  });

  it("leaves a server's other suggestions to a later read while its question is open", async () => {
    const checks = createSshTargetChecks({ record: () => {} });
    let allow;
    const first = counted(
      () =>
        new Promise((resolve) => {
          allow = () => resolve(true);
        }),
    );
    const sameServer = counted(() => true);
    const otherServer = counted(() => true);

    const firstVerdict = checks.verify(SERVER, "host-1", "a1", first);
    assert.equal(await checks.verify(SERVER, "host-2", "a2", sameServer), "busy");
    assert.equal(await checks.verify(OTHER_SERVER, "host-1", "a1", otherServer), true);
    allow();

    assert.equal(await firstVerdict, true);
    assert.equal(await checks.verify(SERVER, "host-2", "a2", sameServer), true);
    assert.equal(sameServer.asked, 1, "asked once the open question is answered");
  });

  it("leaves one refused token to the page, then backs off when the next is refused too", async () => {
    const checks = createSshTargetChecks({ record: () => {} });
    const steps = counted(() => true, "refused");

    assert.equal(await checks.verify(SERVER, "host-1", "my-box", steps), "refused");
    assert.equal(await checks.verify(SERVER, "host-1", "my-box", steps), false);
    assert.equal(await checks.verify(SERVER, "host-1", "my-box", steps), false);
    assert.equal(steps.asked, 1);
    assert.equal(steps.probes, 2, "the back-off holds the third read");
  });

  it("forgets a refusal after five minutes, so a later one is renewed too", async () => {
    let clock = 0;
    const checks = createSshTargetChecks({ record: () => {}, now: () => clock });
    const steps = counted(() => true, "refused");

    assert.equal(await checks.verify(SERVER, "host-1", "my-box", steps), "refused");
    clock += RETRY_MS;
    assert.equal(await checks.verify(SERVER, "host-1", "my-box", steps), "refused");
  });

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
