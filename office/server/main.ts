// Entry point for the local office service. Connects to an already running
// Omnigent instance; never starts one.

import { existsSync } from "node:fs";
import { createServer } from "node:http";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { BuildingService } from "./building.ts";
import { MCP_PATH, createOfficeHandler, createUpgradeHandler } from "./http.ts";
import { desktopNotifier, silentNotifier } from "./notifier.ts";
import { OmnigentClient } from "./omnigentClient.ts";
import { StateStore } from "./stateStore.ts";
import { TroubleshootService } from "./troubleshoot.ts";
import { WorkflowService } from "./workflow.ts";

const environment = process.env;
const port = Number(environment.OMNIGENT_OFFICE_PORT ?? 6780);
const devServerPort = Number(environment.OMNIGENT_OFFICE_DEV_PORT ?? 6781);
const omnigentUrl = environment.OMNIGENT_URL ?? "http://127.0.0.1:6767";
const dataDir = environment.OMNIGENT_OFFICE_DATA_DIR ?? join(homedir(), ".omnigent-office");
const staticDir = resolve(dirname(fileURLToPath(import.meta.url)), "..", "dist");

const client = new OmnigentClient({
  baseUrl: omnigentUrl,
  token: environment.OMNIGENT_TOKEN,
  userEmail: environment.OMNIGENT_USER_EMAIL,
});
const store = new StateStore(join(dataDir, "office-state.json"));
const notifier = environment.OMNIGENT_OFFICE_NOTIFICATIONS === "off" ? silentNotifier : desktopNotifier();
const building = new BuildingService(client, store, undefined, { notifier });
// Role sessions run on hosts of this machine and reach the office over loopback.
const workflow = new WorkflowService({
  omnigent: client,
  store,
  notifier,
  mcpUrl: environment.OMNIGENT_OFFICE_MCP_URL ?? `http://127.0.0.1:${port}${MCP_PATH}`,
  clock: () => Math.floor(Date.now() / 1000),
});
building.attachWorkflow(workflow);
const troubleshooter = new TroubleshootService({ building, workflow, client, store, notifier, clock: () => Math.floor(Date.now() / 1000) });
building.attachProblemCounts((projectId) => troubleshooter.problemCount(projectId));
const handler = createOfficeHandler({
  building,
  workflow,
  client,
  troubleshooter,
  allowedPorts: [port, devServerPort],
  staticDir: existsSync(join(staticDir, "index.html")) ? staticDir : null,
});

const server = createServer((request, response) => {
  void handler(request, response);
});
server.on("upgrade", createUpgradeHandler({ client, allowedPorts: [port, devServerPort] }));

server.listen(port, "127.0.0.1", () => {
  console.log(`Omnigent office service on http://127.0.0.1:${port}`);
  console.log(`Connected Omnigent: ${omnigentUrl}`);
  console.log(`State file: ${store.path}`);
});

// Keep activity and basement deadlines current while no browser is open.
setInterval(() => {
  void building.getSnapshot(true);
}, 60_000).unref();

// Notice failing agents and run scheduled retries while no browser is open.
void troubleshooter.tick();
setInterval(() => {
  void troubleshooter.tick();
}, 20_000).unref();
