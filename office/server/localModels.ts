// Model lists from harness CLIs on this machine, for harnesses Omnigent's
// host probe cannot list. Used only when the selected host is this machine.

import { execFile } from "node:child_process";
import { hostname } from "node:os";
import { type ModelChoice, parseAgyModels, parseCopilotConfigModels } from "../shared/harnessModels.ts";

const CLI_TIMEOUT_MS = 30_000;

const LOCAL_LISTINGS: Record<string, { command: string; args: string[]; parse: (output: string) => ModelChoice[] }> = {
  "antigravity-native": { command: "agy", args: ["models"], parse: parseAgyModels },
  copilot: { command: "copilot", args: ["help", "config"], parse: parseCopilotConfigModels },
};

/** Runs the harness CLI's model listing; null when the CLI is missing or fails. */
export function localCliModels(harness: string): Promise<ModelChoice[] | null> {
  const listing = LOCAL_LISTINGS[harness];
  if (!listing) return Promise.resolve(null);
  return new Promise((resolve) => {
    execFile(listing.command, listing.args, { timeout: CLI_TIMEOUT_MS, maxBuffer: 1024 * 1024 }, (error, stdout) => {
      if (error) return resolve(null);
      const models = listing.parse(stdout);
      resolve(models.length ? models : null);
    });
  });
}

function shortName(name: string): string {
  return name.trim().toLowerCase().replace(/\.local$/, "");
}

/** Whether an Omnigent host name names this machine. */
export function isThisMachine(hostName: string | undefined, machineName: string = hostname()): boolean {
  return Boolean(hostName) && shortName(hostName as string) === shortName(machineName);
}
