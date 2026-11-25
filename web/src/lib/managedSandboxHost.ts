// The id a managed create gives its new sandbox host. The client mints it so the
// create and the session's requests key by that host from the first request.

import { resolveServerInfo, type ServerInfo } from "./capabilities";
import { randomUUID } from "./randomUUID";

/** How long a submit waits for a still-loading `/v1/info` probe before sending no id. */
const PROBE_WAIT_MS = 3_000;

/**
 * A fresh 32-hex host id for a managed JSON create, fork or move, or `undefined`
 * unless `/v1/info` advertises `request_managed_sandbox_host_id`. While the probe
 * is loading, waits up to `PROBE_WAIT_MS`; a failed or slow probe sends no id.
 */
export async function managedSandboxHostId(
  info: ServerInfo | "loading",
): Promise<string | undefined> {
  const resolved = info === "loading" ? await probeWithTimeout() : info;
  if (resolved?.request_managed_sandbox_host_id !== true) return undefined;
  return randomUUID().replace(/-/g, "");
}

async function probeWithTimeout(): Promise<ServerInfo | undefined> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<undefined>((resolve) => {
    timer = setTimeout(() => resolve(undefined), PROBE_WAIT_MS);
  });
  try {
    return await Promise.race([resolveServerInfo(), timeout]);
  } finally {
    clearTimeout(timer);
  }
}
