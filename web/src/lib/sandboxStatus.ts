import type { SandboxLaunchStage } from "./types";

// `starting` is the sandbox host dialing back; `connecting` is its runner launch.
const SANDBOX_STAGE_LABELS = new Map(
  Object.entries({
    provisioning: "Provisioning sandbox",
    cloning: "Cloning repository",
    starting: "Connecting host",
    connecting: "Starting agent",
    ready: "Connected",
    failed: "Sandbox launch failed",
  } satisfies Record<SandboxLaunchStage, string>),
);

/** Shared launch copy, including stages introduced by a newer server. */
export function sandboxStageLabel(stage: string): string {
  return SANDBOX_STAGE_LABELS.get(stage) ?? "Launching sandbox";
}
