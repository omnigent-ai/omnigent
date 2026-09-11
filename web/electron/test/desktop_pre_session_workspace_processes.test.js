"use strict";

const { spawn, spawnSync } = require("node:child_process");
const { afterEach, describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const REPO_ROOT = path.resolve(__dirname, "../../..");
const STOP_SCRIPT = path.join(REPO_ROOT, "web/electron/e2e/stop_desktop_pre_session_workspace.sh");
const PROCESS_HELPER = path.join(
  REPO_ROOT,
  "web/electron/e2e/desktop_pre_session_workspace_processes.sh",
);

describe("desktop pre-session demo processes", { skip: process.platform === "win32" }, () => {
  const tempRoots = [];
  const children = [];

  afterEach(() => {
    for (const child of children.splice(0)) {
      if (child.exitCode === null) child.kill("SIGKILL");
    }
    for (const root of tempRoots.splice(0)) {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  function makeTempRoot() {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omni-demo-process-test-"));
    tempRoots.push(root);
    return root;
  }

  function demoRoot(tempRoot) {
    return path.join(tempRoot, `omnigent-prechat-manual-demo-${process.getuid()}`);
  }

  it("rejects process state instead of executing it", () => {
    const tempRoot = makeTempRoot();
    const root = demoRoot(tempRoot);
    const marker = path.join(tempRoot, "executed");
    fs.mkdirSync(root, { mode: 0o700 });
    fs.writeFileSync(
      path.join(root, "processes.env"),
      `mock_pid=$(touch ${JSON.stringify(marker)})\npage_pid=2\nomnidev_pid=3\nelectron_pid=4\n`,
      { mode: 0o600 },
    );

    const result = spawnSync("bash", [STOP_SCRIPT], {
      env: { ...process.env, TMPDIR: tempRoot },
      encoding: "utf8",
    });

    assert.notEqual(result.status, 0);
    assert.equal(fs.existsSync(marker), false);
    assert.match(result.stderr, /Invalid demo process file/);
  });

  it("refuses a symlinked demo root without altering its target", () => {
    const tempRoot = makeTempRoot();
    const linkedTarget = path.join(tempRoot, "linked-target");
    const stateFile = path.join(linkedTarget, "processes.env");
    const state = "untrusted retained state\n";
    fs.mkdirSync(linkedTarget);
    fs.writeFileSync(stateFile, state);
    fs.symlinkSync(linkedTarget, demoRoot(tempRoot));

    const result = spawnSync("bash", [STOP_SCRIPT], {
      env: { ...process.env, TMPDIR: tempRoot },
      encoding: "utf8",
    });

    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /Refusing unsafe demo directory/);
    assert.equal(fs.readFileSync(stateFile, "utf8"), state);
  });

  it("stops every validated process in retained state", async () => {
    const tempRoot = makeTempRoot();
    const root = demoRoot(tempRoot);
    fs.mkdirSync(root, { mode: 0o700 });
    const exited = [];
    for (let index = 0; index < 4; index += 1) {
      const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"]);
      children.push(child);
      exited.push(new Promise((resolve) => child.once("exit", resolve)));
    }
    fs.writeFileSync(
      path.join(root, "processes.env"),
      [
        `mock_pid=${children[0].pid}`,
        `page_pid=${children[1].pid}`,
        `omnidev_pid=${children[2].pid}`,
        `electron_pid=${children[3].pid}`,
        "",
      ].join("\n"),
      { mode: 0o600 },
    );

    const result = spawnSync("bash", [STOP_SCRIPT], {
      env: { ...process.env, TMPDIR: tempRoot },
      encoding: "utf8",
    });

    assert.equal(result.status, 0, result.stderr);
    await Promise.all(exited);
    assert.equal(fs.existsSync(path.join(root, "processes.env")), false);
  });

  it("reports both unavailable services after readiness is exhausted", () => {
    const result = spawnSync(
      "bash",
      [
        "-c",
        'source "$1"; wait_for_demo_services http://127.0.0.1:1 http://127.0.0.1:2 1 0',
        "test",
        PROCESS_HELPER,
      ],
      { encoding: "utf8" },
    );

    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /Workspace server or host did not become ready/);
    assert.match(result.stderr, /Mock model server did not become ready/);
  });

  it("cleans tracked processes when setup exits before state transfer", async () => {
    const tempRoot = makeTempRoot();
    const pidFile = path.join(tempRoot, "pid");
    const launcher = spawn(
      "bash",
      [
        "-c",
        [
          'source "$1"',
          'mkdir "$2"',
          'begin_demo_process_ownership "$2"',
          "sleep 60 & mock_pid=$!",
          'printf "%s\\n" "$mock_pid" > "$3"',
          "exit 9",
        ].join("; "),
        "test",
        PROCESS_HELPER,
        path.join(tempRoot, "launch-lock"),
        pidFile,
      ],
      { stdio: "ignore" },
    );

    const [status] = await Promise.all([
      new Promise((resolve) => launcher.once("exit", resolve)),
      new Promise((resolve, reject) => {
        const deadline = Date.now() + 2000;
        const poll = () => {
          if (fs.existsSync(pidFile)) resolve();
          else if (Date.now() >= deadline) reject(new Error("tracked PID was not recorded"));
          else setTimeout(poll, 10);
        };
        poll();
      }),
    ]);

    assert.equal(status, 9);
    const pid = Number(fs.readFileSync(pidFile, "utf8"));
    assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
  });
});
