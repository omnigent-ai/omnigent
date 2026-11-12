import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { createAskpass, type AskpassRequest } from "../src/ssh_askpass.ts";

/** Run the helper the way ssh does: SSH_ASKPASS with the prompt as its argument. */
function askLikeSsh(
  env: Record<string, string>,
  prompt: string,
  extraEnv: Record<string, string> = {},
): Promise<{ code: unknown; stdout: string }> {
  return new Promise((resolve) => {
    execFile(
      String(env.SSH_ASKPASS),
      [prompt],
      { env: { ...process.env, ...env, ...extraEnv } },
      (error, stdout) => {
        resolve({ code: error ? (error.code ?? 1) : 0, stdout });
      },
    );
  });
}

describe("createAskpass", () => {
  it("answers ssh's prompt from the app, and exits 1 when the user cancels", async (t) => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "askpass-test-"));
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const seen: AskpassRequest[] = [];
    // In tests the app binary is plain node, which ignores ELECTRON_RUN_AS_NODE.
    const askpass = await createAskpass({
      dir,
      execPath: process.execPath,
      onPrompt: async (request) => {
        seen.push(request);
        return request.prompt.includes("password") ? "s3cret" : null;
      },
    });
    t.after(() => askpass.close());

    assert.deepEqual(await askLikeSsh(askpass.env, "pat@my-ec2's password: "), {
      code: 0,
      stdout: "s3cret\n",
    });
    assert.equal(
      (await askLikeSsh(askpass.env, "Trust?", { SSH_ASKPASS_PROMPT: "confirm" })).code,
      1,
    );
    assert.deepEqual(
      seen.map(({ prompt, hint }) => ({ prompt, hint })),
      [
        { prompt: "pat@my-ec2's password: ", hint: "" },
        { prompt: "Trust?", hint: "confirm" },
      ],
    );
    assert.equal(askpass.env.SSH_ASKPASS_REQUIRE, "force");
    assert.equal(fs.statSync(String(askpass.env.OMNIGENT_SSH_ASKPASS_SOCKET)).mode & 0o077, 0);
  });

  // Without the withdrawal the wait never ends, so the timeout makes it a failure.
  it(
    "withdraws the question when ssh's helper exits before an answer",
    { timeout: 10000 },
    async (t) => {
      const dir = fs.mkdtempSync(path.join(os.tmpdir(), "askpass-test-"));
      t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
      let prompted: (request: AskpassRequest) => void = () => {};
      const asked = new Promise<AskpassRequest>((resolve) => {
        prompted = resolve;
      });
      const askpass = await createAskpass({
        dir,
        execPath: process.execPath,
        // The user never answers.
        onPrompt: (request) => {
          prompted(request);
          return new Promise(() => {});
        },
      });
      t.after(() => askpass.close());
      const helper = execFile(String(askpass.env.SSH_ASKPASS), ["password: "], {
        env: { ...process.env, ...askpass.env },
      });

      const { signal } = await asked;
      assert.equal(signal.aborted, false);
      helper.kill();

      await new Promise((resolve) => {
        signal.addEventListener("abort", resolve);
      });
    },
  );

  it("refuses a caller without the token", async (t) => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "askpass-test-"));
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    let asked = false;
    const askpass = await createAskpass({
      dir,
      execPath: process.execPath,
      onPrompt: async () => {
        asked = true;
        return "s3cret";
      },
    });
    t.after(() => askpass.close());

    // A wrong token, and one the same length in characters but not in bytes.
    for (const token of ["0".repeat(48), "\u00e9".repeat(48)]) {
      // Sequential by design: each caller is refused before the next.
      // oxlint-disable-next-line no-await-in-loop
      const result = await askLikeSsh(askpass.env, "password: ", {
        OMNIGENT_SSH_ASKPASS_TOKEN: token,
      });
      assert.deepEqual(result, { code: 1, stdout: "" });
    }
    assert.equal(asked, false);
  });
});
