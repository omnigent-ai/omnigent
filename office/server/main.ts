// Entry point for the local office service. Connects to an already running
// Omnigent instance; never starts one.

import { existsSync } from "node:fs";
import { createServer } from "node:http";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { BuildingService } from "./building.ts";
import { createOfficeHandler } from "./http.ts";
import { OmnigentClient } from "./omnigentClient.ts";
import { StateStore } from "./stateStore.ts";

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
const building = new BuildingService(client, store);
const handler = createOfficeHandler({
  building,
  client,
  allowedPorts: [port, devServerPort],
  staticDir: existsSync(join(staticDir, "index.html")) ? staticDir : null,
});

const server = createServer((request, response) => {
  void handler(request, response);
});

server.listen(port, "127.0.0.1", () => {
  console.log(`Omnigent office service on http://127.0.0.1:${port}`);
  console.log(`Connected Omnigent: ${omnigentUrl}`);
  console.log(`State file: ${store.path}`);
});

// Keep activity and basement deadlines current while no browser is open.
setInterval(() => {
  void building.getSnapshot(true);
}, 60_000).unref();
