"use strict";

const { spawn, spawnSync } = require("node:child_process");
const { once } = require("node:events");
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
  const childPids = [];

  afterEach(() => {
    for (const child of children.splice(0)) {
      if (child.exitCode === null) child.kill("SIGKILL");
    }
    for (const pid of childPids.splice(0)) {
      try {
        process.kill(pid, "SIGKILL");
      } catch (error) {
        if (error.code !== "ESRCH") throw error;
      }
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

  function startToken(pid) {
    const result = spawnSync("ps", ["-p", String(pid), "-o", "lstart="], {
      encoding: "utf8",
      env: { ...process.env, LC_ALL: "C" },
    });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.replaceAll(/[^a-z0-9]/gi, "");
  }

  async function waitForPath(filePath, deadline) {
    if (fs.existsSync(filePath)) return;
    if (Date.now() >= deadline) throw new Error(`${filePath} was not created`);
    await new Promise((resolve) => {
      setTimeout(resolve, 10);
    });
    await waitForPath(filePath, deadline);
  }

  function stopWithFixtureCommands(root, stateFile, commandPrefix) {
    return spawnSync(
      "bash",
      [
        "-c",
        [
          'source "$1"',
          'demo_root="$2"',
          'repo_root="$3"',
          'python="$4"',
          'expect="$4"',
          'electron="$4"',
          'load_demo_process_state "$5"',
          "stop_demo_processes",
          'rm -f "$5"',
        ].join("; "),
        "test",
        PROCESS_HELPER,
        root,
        REPO_ROOT,
        commandPrefix,
        stateFile,
      ],
      { encoding: "utf8" },
    );
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

  it("stops every validated process in retained state", { timeout: 5000 }, async () => {
    const tempRoot = makeTempRoot();
    const root = demoRoot(tempRoot);
    fs.mkdirSync(root, { mode: 0o700 });
    const idleCode = "setInterval(()=>{},1e3)";
    const commandPrefix = `${process.execPath} -e ${idleCode} --`;
    const mockPort = 43121;
    const pagePort = 43122;
    const commands = [
      [path.join(REPO_ROOT, "tests/server/integration/mock_llm_server.py"), String(mockPort)],
      [
        "-m",
        "http.server",
        String(pagePort),
        "--bind",
        "127.0.0.1",
        "--directory",
        path.join(root, "page"),
      ],
      [path.join(root, "omnidev.exp")],
      [
        path.join(REPO_ROOT, "web/electron"),
        `--user-data-dir=${path.join(root, "electron-profile")}`,
      ],
    ];
    const owned = commands.map((args) => spawn(process.execPath, ["-e", idleCode, "--", ...args]));
    children.push(...owned);
    await Promise.all(owned.map((child) => once(child, "spawn")));
    const exited = owned.map(
      (child) =>
        new Promise((resolve) => {
          child.once("exit", resolve);
        }),
    );
    fs.writeFileSync(
      path.join(root, "processes.env"),
      [
        `mock_pid=${children[0].pid}`,
        `mock_port=${mockPort}`,
        `mock_started=${startToken(children[0].pid)}`,
        `page_pid=${children[1].pid}`,
        `page_port=${pagePort}`,
        `page_started=${startToken(children[1].pid)}`,
        `omnidev_pid=${children[2].pid}`,
        `omnidev_started=${startToken(children[2].pid)}`,
        `electron_pid=${children[3].pid}`,
        `electron_started=${startToken(children[3].pid)}`,
        "",
      ].join("\n"),
      { mode: 0o600 },
    );

    const stateFile = path.join(root, "processes.env");
    const result = stopWithFixtureCommands(root, stateFile, commandPrefix);

    assert.equal(result.status, 0, result.stderr);
    await Promise.all(exited);
    assert.equal(fs.existsSync(path.join(root, "processes.env")), false);
  });

  it("leaves a reused PID and its child untouched", { timeout: 5000 }, async () => {
    const tempRoot = makeTempRoot();
    const root = demoRoot(tempRoot);
    const stateFile = path.join(root, "processes.env");
    const childPidFile = path.join(tempRoot, "child.pid");
    fs.mkdirSync(root, { mode: 0o700 });
    const parentCode = [
      "const{spawn}=require('node:child_process')",
      "const{writeFileSync}=require('node:fs')",
      "const child=spawn(process.execPath,['-e','setInterval(()=>{},1e3)'])",
      "writeFileSync(process.env.CHILD_PID_FILE,String(child.pid))",
      "setInterval(()=>{},1e3)",
    ].join(";");
    const commandPrefix = `${process.execPath} -e ${parentCode} --`;
    const parent = spawn(
      process.execPath,
      [
        "-e",
        parentCode,
        "--",
        path.join(REPO_ROOT, "web/electron"),
        `--user-data-dir=${path.join(root, "electron-profile")}`,
      ],
      { env: { ...process.env, CHILD_PID_FILE: childPidFile } },
    );
    children.push(parent);
    await once(parent, "spawn");
    await waitForPath(childPidFile, Date.now() + 2000);
    const childPid = Number(fs.readFileSync(childPidFile, "utf8"));
    childPids.push(childPid);
    const currentStarted = startToken(parent.pid);
    fs.writeFileSync(
      stateFile,
      [
        `mock_pid=${parent.pid}`,
        "mock_port=43121",
        `mock_started=${currentStarted}`,
        `page_pid=${parent.pid}`,
        "page_port=43122",
        `page_started=${currentStarted}`,
        `omnidev_pid=${parent.pid}`,
        `omnidev_started=${currentStarted}`,
        `electron_pid=${parent.pid}`,
        "electron_started=MonJan010000001990",
        "",
      ].join("\n"),
      { mode: 0o600 },
    );

    const result = stopWithFixtureCommands(root, stateFile, commandPrefix);

    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stderr, /Skipping stale electron PID/);
    assert.equal(process.kill(parent.pid, 0), true);
    assert.equal(process.kill(childPid, 0), true);
    assert.equal(fs.existsSync(stateFile), false);
  });

  it("continues valid cleanup after a descendant is reparented", { timeout: 5000 }, async () => {
    const tempRoot = makeTempRoot();
    const root = demoRoot(tempRoot);
    const stateFile = path.join(root, "processes.env");
    const childPidFile = path.join(tempRoot, "child.pid");
    const completedFile = path.join(tempRoot, "completed");
    fs.mkdirSync(root, { mode: 0o700 });
    const parentCode = [
      "const{spawn}=require('node:child_process')",
      "const{writeFileSync}=require('node:fs')",
      "const child=spawn(process.execPath,['-e','setInterval(()=>{},1e3)'])",
      "writeFileSync(process.env.CHILD_PID_FILE,String(child.pid))",
      "setInterval(()=>{},1e3)",
    ].join(";");
    const idleCode = "setInterval(()=>{},1e3)";
    const electronPrefix = `${process.execPath} -e ${parentCode} --`;
    const mockPrefix = `${process.execPath} -e ${idleCode} --`;
    const parent = spawn(
      process.execPath,
      [
        "-e",
        parentCode,
        "--",
        path.join(REPO_ROOT, "web/electron"),
        `--user-data-dir=${path.join(root, "electron-profile")}`,
      ],
      { env: { ...process.env, CHILD_PID_FILE: childPidFile } },
    );
    const mockPort = 43121;
    const mock = spawn(process.execPath, [
      "-e",
      idleCode,
      "--",
      path.join(REPO_ROOT, "tests/server/integration/mock_llm_server.py"),
      String(mockPort),
    ]);
    children.push(parent, mock);
    await Promise.all([once(parent, "spawn"), once(mock, "spawn")]);
    await waitForPath(childPidFile, Date.now() + 2000);
    const childPid = Number(fs.readFileSync(childPidFile, "utf8"));
    childPids.push(childPid);
    const mockStarted = startToken(mock.pid);
    fs.writeFileSync(
      stateFile,
      [
        `mock_pid=${mock.pid}`,
        `mock_port=${mockPort}`,
        `mock_started=${mockStarted}`,
        `page_pid=${mock.pid}`,
        "page_port=43122",
        `page_started=${mockStarted}`,
        `omnidev_pid=${mock.pid}`,
        `omnidev_started=${mockStarted}`,
        `electron_pid=${parent.pid}`,
        `electron_started=${startToken(parent.pid)}`,
        "",
      ].join("\n"),
      { mode: 0o600 },
    );
    const parentExited = once(parent, "exit");
    const mockExited = once(mock, "exit");

    const result = spawnSync(
      "bash",
      [
        "-c",
        [
          "set -e",
          'source "$1"',
          'demo_root="$2"',
          'repo_root="$3"',
          'electron="$4"',
          'python="$5"',
          'expect="$5"',
          'load_demo_process_state "$6"',
          'reparented_pid="$7"',
          'demo_process_parent() { if [[ "$1" == "$reparented_pid" ]]; then printf "1\\n"; else ps -p "$1" -o ppid= 2>/dev/null | tr -d "[:space:]"; fi; }',
          "stop_demo_processes",
          'rm -f "$6"',
          'printf "done\\n" > "$8"',
        ].join("; "),
        "test",
        PROCESS_HELPER,
        root,
        REPO_ROOT,
        electronPrefix,
        mockPrefix,
        stateFile,
        String(childPid),
        completedFile,
      ],
      { encoding: "utf8" },
    );

    assert.equal(result.status, 0, result.stderr);
    await Promise.all([parentExited, mockExited]);
    assert.equal(fs.readFileSync(completedFile, "utf8"), "done\n");
    assert.equal(fs.existsSync(stateFile), false);
    assert.equal(process.kill(childPid, 0), true);
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

  it(
    "cleans tracked processes when setup exits before state transfer",
    { timeout: 5000 },
    async () => {
      const tempRoot = makeTempRoot();
      const pidFile = path.join(tempRoot, "pid");
      const launcher = spawn(
        "bash",
        [
          "-c",
          [
            'source "$1"',
            'mkdir "$2"',
            'begin_demo_process_ownership "$2" "$3" "$4"',
            'python="$5 -e $7 --"',
            'expect="$python"',
            'electron="$python"',
            "mock_port=43121",
            '"$5" -e "$7" -- "$4/tests/server/integration/mock_llm_server.py" "$mock_port" & mock_pid=$!',
            'mock_started="$(demo_process_start_token "$mock_pid")"',
            'printf "%s\\n" "$mock_pid" > "$6"',
            "exit 9",
          ].join("; "),
          "test",
          PROCESS_HELPER,
          path.join(tempRoot, "launch-lock"),
          demoRoot(tempRoot),
          REPO_ROOT,
          process.execPath,
          pidFile,
          "setInterval(()=>{},1e3)",
        ],
        { stdio: "ignore" },
      );
      children.push(launcher);

      const [status] = await Promise.all([
        new Promise((resolve) => {
          launcher.once("exit", resolve);
        }),
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
      childPids.push(pid);
      assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
    },
  );
});
