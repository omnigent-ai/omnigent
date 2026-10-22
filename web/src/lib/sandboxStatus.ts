/**
 * Copy for each in-flight managed-sandbox launch stage, in pipeline
 * order: provisioning → cloning (repo workspaces only) → starting →
 * connecting. `starting` is the in-sandbox host booting and dialing
 * back to the server (so it reads "Connecting host"); `connecting` is
 * the agent runner being launched on that host (so it reads "Starting
 * agent"). Terminal stages are absent on purpose — `ready` clears the
 * launch state and `failed` renders its own error copy.
 */
export const SANDBOX_STAGE_LABELS: Record<string, string | undefined> = {
  provisioning: "Provisioning sandbox",
  // Still provisioning; `SandboxActionBanner` says what the user needs to do.
  action_required: "Provisioning sandbox",
  cloning: "Cloning repository",
  starting: "Connecting host",
  connecting: "Starting agent",
};
