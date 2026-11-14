"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { launchDesktop } = require("./desktopHarness");

const THIS_MACHINE_HOST_ID = "a".repeat(32);

// Launch with a live daemon enrolled for this machine; only `backend.sessionId`
// reports this host ID, every other session reports a remote host.
async function launchThisMachineDesktop({ backend, scratch, recordDir, preload = [] }) {
  const machineHome = path.join(scratch, "machine");
  const stateDir = path.join(machineHome, ".omnigent");
  fs.mkdirSync(path.join(stateDir, "daemons"), { recursive: true });
  fs.writeFileSync(
    path.join(stateDir, "config.yaml"),
    `host:\n  host_id: ${THIS_MACHINE_HOST_ID}\n`,
  );
  // Disk-backed enrollment and a live PID exercise the production predicate.
  fs.writeFileSync(
    path.join(stateDir, "daemons", "local.json"),
    JSON.stringify({
      pid: process.pid,
      target: backend.serverUrl,
      server_url: backend.serverUrl,
      mode: "server",
      host_id: THIS_MACHINE_HOST_ID,
    }),
  );
  const desktop = await launchDesktop({
    serverUrl: backend.serverUrl,
    recordDir,
    env: { HOME: machineHome, OMNIGENT_CONFIG_HOME: stateDir },
    preload,
  });
  let sessionIsLocal = true;
  try {
    await desktop.window.route(/\/v1\/sessions\/[a-f0-9]+(?:\?.*)?$/, async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      const response = await route.fetch();
      const body = await response.json();
      const local = body.id === backend.sessionId && sessionIsLocal;
      await route.fulfill({
        response,
        json: { ...body, host_id: local ? THIS_MACHINE_HOST_ID : "b".repeat(32) },
      });
    });
  } catch (error) {
    // The caller only cleans up a desktop this helper returns.
    await desktop.electronApp.close().catch(() => {});
    await desktop.stopDisplayCapture().catch(() => {});
    fs.rmSync(desktop.userDataDir, { recursive: true, force: true });
    throw error;
  }
  return {
    desktop,
    setSessionIsLocal: (value) => {
      sessionIsLocal = value;
    },
  };
}

/** Open a session and add a Browser tab from the workspace rail's "Open new" menu. */
async function openSessionBrowserTab(window, serverUrl, sessionId) {
  await window.goto(`${serverUrl}/c/${sessionId}`);
  await window.getByRole("button", { name: /^(Expand|Collapse) right panel$/ }).waitFor();
  const expand = window.getByRole("button", { name: "Expand right panel", exact: true });
  if (await expand.isVisible()) await expand.click();
  await window.getByRole("button", { name: "Open new", exact: true }).click();
  await window.getByRole("menuitem", { name: "Browser", exact: true }).click();
}

module.exports = { THIS_MACHINE_HOST_ID, launchThisMachineDesktop, openSessionBrowserTab };
