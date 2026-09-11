"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");

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

module.exports = { startPodServices, startWorkspaceFixtures };
