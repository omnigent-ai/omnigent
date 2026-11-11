import { performance } from "node:perf_hooks";

export const SESSION_HOST_PROTOCOL = 1;

/** The local identity and enrollment reads from `omnigent_cli`, injected so this loads without it. */
export interface HostIdentityCli {
  readLocalHostId: () => string | null | undefined;
  liveDaemonRecordFor: (
    serverUrl: string,
  ) => { host_id?: string | null | undefined } | null | undefined;
}

/** A session's host and the server it is on. */
export interface HostContext {
  hostId: string | null | undefined;
  serverUrl: string;
}

export interface HostThisMachineCheckerOptions {
  cli: HostIdentityCli;
  now?: () => number;
}

/** Return whether the host is this machine and its live daemon serves the server.
 * Records without host_id are accepted; host-ID equality routes, enrollment authenticates. */
export function isHostThisMachine(
  { hostId, serverUrl }: HostContext,
  cli: HostIdentityCli,
): boolean {
  if (!hostId || hostId !== cli.readLocalHostId()) return false;
  const record = cli.liveDaemonRecordFor(serverUrl);
  if (record === null || record === undefined) return false;
  const recordHostId = record.host_id?.replace(/^host_/, "");
  return !recordHostId || recordHostId === hostId;
}

/** Cache local identity/enrollment checks for at most one second per host/server pair. */
export function createHostThisMachineChecker({
  cli,
  now = () => performance.now(),
}: HostThisMachineCheckerOptions): (context: HostContext) => boolean {
  const snapshots = new Map<string, { eligible: boolean; expiresAt: number }>();
  return ({ hostId, serverUrl }) => {
    if (!hostId) return false;
    const timestamp = now();
    const key = JSON.stringify([hostId, serverUrl]);
    const cached = snapshots.get(key);
    if (cached && timestamp < cached.expiresAt) return cached.eligible;
    for (const [cachedKey, snapshot] of snapshots) {
      if (timestamp >= snapshot.expiresAt) snapshots.delete(cachedKey);
    }
    const eligible = isHostThisMachine({ hostId, serverUrl }, cli);
    snapshots.set(key, { eligible, expiresAt: timestamp + 1000 });
    return eligible;
  };
}
