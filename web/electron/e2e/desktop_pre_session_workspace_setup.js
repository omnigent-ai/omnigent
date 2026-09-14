"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");

function isolatedChildEnv(overrides = {}, source = process.env) {
  // Inherit only process basics; provider credentials and runtime injection
  // settings must not leak into the mock services.
  const keys = ["PATH", "HOME", "SHELL", "TERM", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"];
  const base = Object.fromEntries(
    keys.filter((key) => source[key] !== undefined).map((key) => [key, source[key]]),
  );
  return { ...base, ...overrides };
}

async function stopProcess(proc) {
  if (!proc.pid || proc.exitCode !== null || proc.signalCode !== null) return;
  let timer;
  let onExit;
  const exited = new Promise((resolve) => {
    onExit = resolve;
    proc.once("exit", onExit);
  });
  try {
    proc.kill("SIGTERM");
    await Promise.race([
      exited,
      new Promise((resolve) => {
        timer = setTimeout(resolve, 10_000);
      }),
    ]);
    if (proc.exitCode === null && proc.signalCode === null) {
      proc.kill("SIGKILL");
      await exited;
    }
  } finally {
    clearTimeout(timer);
    proc.removeListener("exit", onExit);
  }
}

async function startPodServices({ mock, pod, waitForMock, configureMock, waitForPod }) {
  const processes = [];
  const logs = [];
  let closing;
  let rejectSpawn;
  const spawnFailure = new Promise((_resolve, reject) => {
    rejectSpawn = reject;
  });
  const launch = (service) => {
    const log = fs.openSync(service.logPath, "w");
    logs.push(log);
    const proc = spawn(service.command, service.args, {
      ...service.options,
      stdio: ["ignore", log, log],
    });
    proc.once("error", rejectSpawn);
    processes.push(proc);
    return proc;
  };
  const close = () => {
    closing ??= (async () => {
      try {
        await Promise.all(processes.map(stopProcess));
      } finally {
        for (const log of logs) fs.closeSync(log);
      }
    })();
    return closing;
  };
  try {
    const mockProc = launch(mock);
    await Promise.race([waitForMock(mockProc, mock.logPath), spawnFailure]);
    await Promise.race([configureMock(), spawnFailure]);
    const podProc = launch(pod);
    const hosts = await Promise.race([waitForPod(podProc, pod.logPath), spawnFailure]);
    return { hosts, close };
  } catch (error) {
    await close();
    throw error;
  }
}

async function startWorkspaceFixtures(startPod, startPage) {
  const pod = await startPod();
  try {
    return { pod, demoPage: await startPage() };
  } catch (error) {
    await pod.close();
    throw error;
  }
}

async function resolveShellWindow(electronApp, serverUrl, options = {}) {
  const expectedOrigin = new URL(serverUrl).origin;
  const timeoutMs = options.timeoutMs ?? 60_000;
  const pollMs = options.pollMs ?? 100;
  const deadline = Date.now() + timeoutMs;
  const resolveCurrent = async () => {
    const pages = electronApp.windows();
    const shell = pages.find((page) => {
      try {
        return new URL(page.url()).origin === expectedOrigin;
      } catch {
        return false;
      }
    });
    if (shell) return shell;
    if (Date.now() >= deadline) {
      throw new Error(
        `no shell window appeared on ${expectedOrigin}; windows: ${pages
          .map((page) => page.url())
          .join(", ")}`,
      );
    }
    await new Promise((resolve) => {
      setTimeout(resolve, Math.min(pollMs, Math.max(0, deadline - Date.now())));
    });
    return resolveCurrent();
  };
  return resolveCurrent();
}

module.exports = { isolatedChildEnv, resolveShellWindow, startPodServices, startWorkspaceFixtures };
