"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { execFile, spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createAskpass } = require("../src/ssh_askpass");

const HOST_KEY_PROMPT =
  "The authenticity of host 'my-ec2 (10.0.0.7)' can't be established.\n" +
  "ED25519 key fingerprint is SHA256:abc.\n" +
  "Are you sure you want to continue connecting (yes/no/[fingerprint])? ";

/** Run the script the way ssh does: SSH_ASKPASS with the prompt as its argument. */
function askLikeSsh(env, prompt, extraEnv = {}) {
  return new Promise((resolve) => {
    execFile(
      env.SSH_ASKPASS,
      [prompt],
      { env: { ...process.env, ...env, ...extraEnv } },
      (error, stdout) => {
        resolve({ code: error ? (error.code ?? 1) : 0, stdout });
      },
    );
  });
}

function makeDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "askpass-test-"));
  fs.chmodSync(dir, 0o700);
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

/** Resolve once `check()` is true, polling until `deadlineMs` passes. */
async function waitFor(check, deadlineMs = 5000) {
  const deadline = Date.now() + deadlineMs;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out waiting for the condition");
    // oxlint-disable-next-line no-await-in-loop -- Polling.
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
  }
}

describe("createAskpass", () => {
  it("answers ssh's prompt from the app, and exits 1 when the user cancels", async (t) => {
    const dir = makeDir(t);
    const seen = [];
    const askpass = await createAskpass({
      dir,
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
    assert.deepEqual(
      await askLikeSsh(askpass.env, HOST_KEY_PROMPT, { SSH_ASKPASS_PROMPT: "confirm" }),
      { code: 1, stdout: "" },
    );
    assert.deepEqual(
      seen.map(({ prompt, hint }) => ({ prompt, hint })),
      [
        { prompt: "pat@my-ec2's password: ", hint: "" },
        { prompt: HOST_KEY_PROMPT, hint: "confirm" },
      ],
    );
    assert.equal(askpass.env.SSH_ASKPASS_REQUIRE, "force");
    // Each question's file and pipe are gone once it is answered.
    assert.deepEqual(fs.readdirSync(dir), ["askpass"]);
  });

  it("gives ssh an empty answer as an answer, not as a cancel", async (t) => {
    const askpass = await createAskpass({ dir: makeDir(t), onPrompt: async () => "" });
    t.after(() => askpass.close());

    assert.deepEqual(await askLikeSsh(askpass.env, "Verification code: "), {
      code: 0,
      stdout: "\n",
    });
  });

  it("delivers the longest answer whole, and cancels a longer one", async (t) => {
    const longest = "x".repeat(4094);
    let answer = longest;
    const askpass = await createAskpass({ dir: makeDir(t), onPrompt: async () => answer });
    t.after(() => askpass.close());

    assert.deepEqual(await askLikeSsh(askpass.env, "password: "), {
      code: 0,
      stdout: `${longest}\n`,
    });
    answer = `${longest}x`;
    assert.deepEqual(await askLikeSsh(askpass.env, "password: "), { code: 1, stdout: "" });
  });

  // The packaged app refuses to run as plain Node, so a script that started
  // the app's binary could never answer.
  it("does not start the app's own binary", async (t) => {
    const askpass = await createAskpass({ dir: makeDir(t), onPrompt: async () => "s3cret" });
    t.after(() => askpass.close());

    const script = fs.readFileSync(askpass.env.SSH_ASKPASS, "utf8");
    assert.equal(script.includes(process.execPath), false);
    assert.doesNotMatch(script, /ELECTRON_RUN_AS_NODE/);
    assert.equal(fs.statSync(askpass.env.SSH_ASKPASS).mode & 0o077, 0);
  });

  // Without the withdrawal the wait never ends, so the timeout makes it a failure.
  it(
    "withdraws the question when ssh's script exits before an answer",
    { timeout: 10000 },
    async (t) => {
      let prompted;
      const asked = new Promise((resolve) => {
        prompted = resolve;
      });
      const askpass = await createAskpass({
        dir: makeDir(t),
        // The user never answers.
        onPrompt: (request) => {
          prompted(request);
          return new Promise(() => {});
        },
      });
      t.after(() => askpass.close());
      const script = execFile(askpass.env.SSH_ASKPASS, ["password: "], {
        env: { ...process.env, ...askpass.env },
      });

      const { signal } = await asked;
      assert.equal(signal.aborted, false);
      script.kill();

      await new Promise((resolve) => {
        signal.addEventListener("abort", resolve);
      });
    },
  );

  // Without the release the script, and the ssh waiting on it, never exit.
  it(
    "releases a waiting script, and withdraws its question, when closed",
    { timeout: 10000 },
    async (t) => {
      let prompted;
      const asked = new Promise((resolve) => {
        prompted = resolve;
      });
      const askpass = await createAskpass({
        dir: makeDir(t),
        onPrompt: (request) => {
          prompted(request);
          return new Promise(() => {});
        },
      });
      t.after(() => askpass.close());
      const exited = askLikeSsh(askpass.env, "password: ");

      const { signal } = await asked;
      askpass.close();

      assert.equal(signal.aborted, true);
      assert.deepEqual(await exited, { code: 1, stdout: "" });
    },
  );

  // Without the script's watch on the app it waits forever on an app that is gone.
  it(
    "lets a waiting script exit when the app quits mid-question",
    { timeout: 20000 },
    async (t) => {
      const dir = makeDir(t);
      const app = spawn(
        process.execPath,
        [
          "-e",
          `require(${JSON.stringify(require.resolve("../src/ssh_askpass"))})
          .createAskpass({
            dir: process.argv[1],
            onPrompt: () => {
              console.log("asked");
              return new Promise(() => {});
            },
          })
          .then(({ env }) => console.log(JSON.stringify(env)));
        setInterval(() => {}, 1000);`,
          dir,
        ],
        { stdio: ["ignore", "pipe", "inherit"] },
      );
      t.after(() => app.kill("SIGKILL"));
      const lines = [];
      app.stdout.on("data", (chunk) => lines.push(...String(chunk).split("\n").filter(Boolean)));
      await waitFor(() => lines.length > 0);
      const env = JSON.parse(lines[0]);
      const exit = { done: null };
      const script = execFile(
        env.SSH_ASKPASS,
        ["password: "],
        { env: { ...process.env, ...env } },
        (error, stdout) => exit.done?.({ code: error ? (error.code ?? 1) : 0, stdout }),
      );
      t.after(() => script.kill("SIGKILL"));
      const exited = new Promise((resolve) => {
        exit.done = resolve;
      });

      // The app says so once it has taken the question.
      await waitFor(() => lines.includes("asked"));
      app.kill("SIGKILL");

      assert.deepEqual(await exited, { code: 1, stdout: "" });
    },
  );

  // The no-answer line must reach a script however late it gets to read its pipe.
  it(
    "lets a script exit when the app dies before answering, even if the script reads late",
    { timeout: 20000 },
    async (t) => {
      const dir = makeDir(t);
      // The script is written; this process stops answering, so nothing ever opens the pipe.
      const askpass = await createAskpass({ dir, onPrompt: async () => "s3cret" });
      askpass.close();
      const app = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
        stdio: "ignore",
      });
      t.after(() => app.kill("SIGKILL"));
      const exit = { done: null };
      const script = execFile(
        askpass.env.SSH_ASKPASS,
        ["password: "],
        { env: { ...process.env, ...askpass.env, OMNIGENT_SSH_ASKPASS_APP_PID: String(app.pid) } },
        (error, stdout) => exit.done?.({ code: error ? (error.code ?? 1) : 0, stdout }),
      );
      t.after(() => script.kill("SIGKILL"));
      const exited = new Promise((resolve) => {
        exit.done = resolve;
      });

      // The question is published; hold the script still while the app dies
      // and the watcher finishes, then let it read.
      await waitFor(() => fs.existsSync(path.join(dir, `q.${script.pid}`)));
      script.kill("SIGSTOP");
      const appGone = new Promise((resolve) => {
        app.once("exit", resolve);
      });
      app.kill("SIGKILL");
      await appGone;
      await new Promise((resolve) => {
        setTimeout(resolve, 2500);
      });
      assert.equal(script.exitCode, null);
      script.kill("SIGCONT");

      assert.deepEqual(await exited, { code: 1, stdout: "" });
      assert.deepEqual(fs.readdirSync(dir), ["askpass"]);
    },
  );

  it("refuses a caller without the token", async (t) => {
    let asked = false;
    const askpass = await createAskpass({
      dir: makeDir(t),
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
